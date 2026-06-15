#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROOT_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-python3}"
HOST="${HOST:-127.0.0.1}"
SYMBOL="${SYMBOL:-BTCUSDT}"
PORT="${PORT:-8000}"
POLL_SECONDS="${POLL_SECONDS:-10}"
KLINE_LIMIT="${KLINE_LIMIT:-300}"
DATA_DIR="${DATA_DIR:-$ROOT_DIR/data}"
DB_PATH="${DB_PATH:-$ROOT_DIR/data/monitor.sqlite3}"
WEBHOOK_URL="${WEBHOOK_URL:-https://event.easy-tx.com/api/signals/ingest}"
WEBHOOK_TOKEN="${WEBHOOK_TOKEN:-}"
WEBHOOK_TIMEOUT="${WEBHOOK_TIMEOUT:-5}"
WARMUP_MONTHS="${WARMUP_MONTHS:-3}"
WARMUP_TIMEOUT="${WARMUP_TIMEOUT:-20}"
STAKE="${STAKE:-10}"
WIN_RETURN="${WIN_RETURN:-}"
STAKE_PROGRESSION="${STAKE_PROGRESSION:-1}"
STAKE_PROGRESSION_MAX_ORDERS="${STAKE_PROGRESSION_MAX_ORDERS:-3}"
NO_WARMUP="${NO_WARMUP:-0}"
NO_PERSISTENCE="${NO_PERSISTENCE:-0}"
NO_WEBHOOK="${NO_WEBHOOK:-0}"
WARMUP_CURRENT_MONTH_DAILY="${WARMUP_CURRENT_MONTH_DAILY:-1}"

usage() {
  cat <<'USAGE'
Usage: scripts/run.sh [SYMBOL] [PORT]
       scripts/run.sh [options]

启动币安事件合约量价监控程序。

参数:
  --symbol SYMBOL        交易对，默认: BTCUSDT
  --host HOST            监听地址，默认: 127.0.0.1
  --port PORT            页面端口，默认: 8000
  --poll-seconds N       币安轮询间隔秒数，默认: 10
  --limit N              每次拉取的 1分钟K线数量，默认: 300
  --data-dir DIR         本地 Binance Vision 缓存目录，默认: ./data
  --db-path PATH         SQLite 持久化路径，默认: ./data/monitor.sqlite3
  --webhook-url URL      外部信号 webhook 地址，默认: event.easy-tx 导入接口
  --webhook-token TOKEN  外部信号 importToken
  --webhook-timeout N    Webhook 超时秒数，默认: 5
  --warmup-months N      预热加载的完整月度文件数量，默认: 3
  --warmup-timeout N     单个历史文件下载超时秒数，默认: 20
  --stake N              基础下单金额，默认: 10
  --win-return N         赢单返还金额，默认: stake * 1.8
  --no-stake-progression 关闭赢单返还滚单
  --stake-progression-max-orders N
                         最大连续滚单次数，默认: 3
  --no-current-month-daily
                         跳过当前月份日线历史预热
  --no-warmup            关闭本地/远程历史预热
  --no-persistence       关闭 SQLite 订单和信号持久化
  --no-webhook           关闭外部 webhook 信号推送
  --python PATH          Python 可执行文件，默认: python3 或 PYTHON_BIN
  -h, --help             显示帮助并退出

环境变量覆盖:
  SYMBOL, HOST, PORT, POLL_SECONDS, KLINE_LIMIT, DATA_DIR, DB_PATH,
  WEBHOOK_URL, WEBHOOK_TOKEN, WEBHOOK_TIMEOUT,
  WARMUP_MONTHS, WARMUP_TIMEOUT, STAKE, WIN_RETURN,
  STAKE_PROGRESSION, STAKE_PROGRESSION_MAX_ORDERS,
  NO_WARMUP, NO_PERSISTENCE, NO_WEBHOOK,
  WARMUP_CURRENT_MONTH_DAILY, PYTHON_BIN

示例:
  bash scripts/run.sh
  bash scripts/run.sh ETHUSDT 8080
  bash scripts/run.sh --symbol BTCUSDT --host 0.0.0.0 --port 8000
  bash scripts/run.sh --no-warmup
USAGE
}

require_value() {
  if [ "$#" -lt 2 ] || [ -z "$2" ]; then
    echo "missing value for $1" >&2
    exit 2
  fi
}

POSITIONAL=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --symbol)
      require_value "$1" "${2:-}"
      SYMBOL="$2"
      shift 2
      ;;
    --symbol=*)
      SYMBOL="${1#*=}"
      shift
      ;;
    --host)
      require_value "$1" "${2:-}"
      HOST="$2"
      shift 2
      ;;
    --host=*)
      HOST="${1#*=}"
      shift
      ;;
    --port)
      require_value "$1" "${2:-}"
      PORT="$2"
      shift 2
      ;;
    --port=*)
      PORT="${1#*=}"
      shift
      ;;
    --poll-seconds)
      require_value "$1" "${2:-}"
      POLL_SECONDS="$2"
      shift 2
      ;;
    --poll-seconds=*)
      POLL_SECONDS="${1#*=}"
      shift
      ;;
    --limit)
      require_value "$1" "${2:-}"
      KLINE_LIMIT="$2"
      shift 2
      ;;
    --limit=*)
      KLINE_LIMIT="${1#*=}"
      shift
      ;;
    --data-dir)
      require_value "$1" "${2:-}"
      DATA_DIR="$2"
      shift 2
      ;;
    --data-dir=*)
      DATA_DIR="${1#*=}"
      shift
      ;;
    --db-path)
      require_value "$1" "${2:-}"
      DB_PATH="$2"
      shift 2
      ;;
    --db-path=*)
      DB_PATH="${1#*=}"
      shift
      ;;
    --webhook-url)
      require_value "$1" "${2:-}"
      WEBHOOK_URL="$2"
      shift 2
      ;;
    --webhook-url=*)
      WEBHOOK_URL="${1#*=}"
      shift
      ;;
    --webhook-token)
      require_value "$1" "${2:-}"
      WEBHOOK_TOKEN="$2"
      shift 2
      ;;
    --webhook-token=*)
      WEBHOOK_TOKEN="${1#*=}"
      shift
      ;;
    --webhook-timeout)
      require_value "$1" "${2:-}"
      WEBHOOK_TIMEOUT="$2"
      shift 2
      ;;
    --webhook-timeout=*)
      WEBHOOK_TIMEOUT="${1#*=}"
      shift
      ;;
    --warmup-months)
      require_value "$1" "${2:-}"
      WARMUP_MONTHS="$2"
      shift 2
      ;;
    --warmup-months=*)
      WARMUP_MONTHS="${1#*=}"
      shift
      ;;
    --warmup-timeout)
      require_value "$1" "${2:-}"
      WARMUP_TIMEOUT="$2"
      shift 2
      ;;
    --warmup-timeout=*)
      WARMUP_TIMEOUT="${1#*=}"
      shift
      ;;
    --stake)
      require_value "$1" "${2:-}"
      STAKE="$2"
      shift 2
      ;;
    --stake=*)
      STAKE="${1#*=}"
      shift
      ;;
    --win-return)
      require_value "$1" "${2:-}"
      WIN_RETURN="$2"
      shift 2
      ;;
    --win-return=*)
      WIN_RETURN="${1#*=}"
      shift
      ;;
    --stake-progression-max-orders)
      require_value "$1" "${2:-}"
      STAKE_PROGRESSION_MAX_ORDERS="$2"
      shift 2
      ;;
    --stake-progression-max-orders=*)
      STAKE_PROGRESSION_MAX_ORDERS="${1#*=}"
      shift
      ;;
    --no-stake-progression)
      STAKE_PROGRESSION="0"
      shift
      ;;
    --no-current-month-daily)
      WARMUP_CURRENT_MONTH_DAILY="0"
      shift
      ;;
    --no-warmup)
      NO_WARMUP="1"
      shift
      ;;
    --no-persistence)
      NO_PERSISTENCE="1"
      shift
      ;;
    --no-webhook)
      NO_WEBHOOK="1"
      shift
      ;;
    --python)
      require_value "$1" "${2:-}"
      PYTHON_BIN="$2"
      shift 2
      ;;
    --python=*)
      PYTHON_BIN="${1#*=}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done

if [ "${#POSITIONAL[@]}" -gt 2 ]; then
  echo "too many positional arguments: ${POSITIONAL[*]}" >&2
  usage >&2
  exit 2
fi

if [ "${#POSITIONAL[@]}" -ge 1 ]; then
  SYMBOL="${POSITIONAL[0]}"
fi
if [ "${#POSITIONAL[@]}" -ge 2 ]; then
  PORT="${POSITIONAL[1]}"
fi

EXTRA_ARGS=()
case "$NO_WARMUP" in
  1|true|TRUE|yes|YES|y|Y|on|ON)
    EXTRA_ARGS+=(--no-warmup)
    ;;
esac
case "$NO_PERSISTENCE" in
  1|true|TRUE|yes|YES|y|Y|on|ON)
    EXTRA_ARGS+=(--no-persistence)
    ;;
esac
case "$NO_WEBHOOK" in
  1|true|TRUE|yes|YES|y|Y|on|ON)
    EXTRA_ARGS+=(--no-webhook)
    ;;
esac
case "$STAKE_PROGRESSION" in
  0|false|FALSE|no|NO|n|N|off|OFF)
    EXTRA_ARGS+=(--no-stake-progression)
    ;;
esac
case "$WARMUP_CURRENT_MONTH_DAILY" in
  0|false|FALSE|no|NO|n|N|off|OFF)
    EXTRA_ARGS+=(--no-current-month-daily)
    ;;
esac

"$PYTHON_BIN" - <<'PY'
import sys

if sys.version_info < (3, 10):
    version = ".".join(map(str, sys.version_info[:3]))
    raise SystemExit(f"Python 3.10+ is required, current version is {version}")
PY

cd "$ROOT_DIR"

exec "$PYTHON_BIN" -m app.server \
  --symbol "$SYMBOL" \
  --host "$HOST" \
  --port "$PORT" \
  --poll-seconds "$POLL_SECONDS" \
  --limit "$KLINE_LIMIT" \
  --data-dir "$DATA_DIR" \
  --db-path "$DB_PATH" \
  --webhook-url "$WEBHOOK_URL" \
  --webhook-token "$WEBHOOK_TOKEN" \
  --webhook-timeout "$WEBHOOK_TIMEOUT" \
  --warmup-months "$WARMUP_MONTHS" \
  --warmup-timeout "$WARMUP_TIMEOUT" \
  --stake "$STAKE" \
  ${WIN_RETURN:+--win-return "$WIN_RETURN"} \
  --stake-progression-max-orders "$STAKE_PROGRESSION_MAX_ORDERS" \
  ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
