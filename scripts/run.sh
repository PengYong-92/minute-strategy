#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROOT_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-python3}"
STRATEGY_BUILD_ID="${STRATEGY_BUILD_ID-}"
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
TRADE_SCORE_THRESHOLD="${TRADE_SCORE_THRESHOLD:-auto}"
WIN_RETURN="${WIN_RETURN:-}"
MAX_OPEN_ORDERS="${MAX_OPEN_ORDERS:-2}"
MAX_OPEN_LONG_ORDERS="${MAX_OPEN_LONG_ORDERS:-1}"
MAX_OPEN_SHORT_ORDERS="${MAX_OPEN_SHORT_ORDERS:-2}"
MIN_ORDER_GAP_MINUTES="${MIN_ORDER_GAP_MINUTES:-2}"
STAKE_PROGRESSION="${STAKE_PROGRESSION:-1}"
ROLLING_EDGE_GUARD="${ROLLING_EDGE_GUARD:-1}"
RESULT_SEQUENCE_GUARD="${RESULT_SEQUENCE_GUARD:-1}"
TIME_PERIOD_GUARD="${TIME_PERIOD_GUARD:-0}"
PROFILE_HEALTH_GUARD="${PROFILE_HEALTH_GUARD:-1}"
RESULT_SEQUENCE_LOSS_STREAK="${RESULT_SEQUENCE_LOSS_STREAK:-3}"
RESULT_SEQUENCE_COOLDOWN_MINUTES="${RESULT_SEQUENCE_COOLDOWN_MINUTES:-20}"
RESULT_SEQUENCE_SCOPE="${RESULT_SEQUENCE_SCOPE:-DIRECTION}"
STAKE_PROGRESSION_MAX_ORDERS="${STAKE_PROGRESSION_MAX_ORDERS:-2}"
STAKE_PROGRESSION_MAX_ACTIVE="${STAKE_PROGRESSION_MAX_ACTIVE:-1}"
STAKE_PROGRESSION_BASE_ONLY_SEGMENTS="${STAKE_PROGRESSION_BASE_ONLY_SEGMENTS-}"
PROFILE_GUARD="${PROFILE_GUARD:-0}"
PROFILE_GUARD_MIN_HISTORY="${PROFILE_GUARD_MIN_HISTORY:-15}"
PROFILE_GUARD_MIN_GROUP_SIZE="${PROFILE_GUARD_MIN_GROUP_SIZE:-2}"
PROFILE_DEGRADATION_COOLDOWN_MINUTES="${PROFILE_DEGRADATION_COOLDOWN_MINUTES:-60}"
OBSERVATION_PROFILE_PROMOTION="${OBSERVATION_PROFILE_PROMOTION:-1}"
OBSERVATION_PROFILE_LOOKBACK_DAYS="${OBSERVATION_PROFILE_LOOKBACK_DAYS:-7}"
OBSERVATION_PROFILE_MIN_SAMPLES="${OBSERVATION_PROFILE_MIN_SAMPLES:-12}"
OBSERVATION_PROFILE_MIN_WIN_RATE="${OBSERVATION_PROFILE_MIN_WIN_RATE:-0.72}"
OBSERVATION_PROFILE_MIN_EV="${OBSERVATION_PROFILE_MIN_EV:-4}"
OBSERVATION_PROFILE_MIN_EDGE="${OBSERVATION_PROFILE_MIN_EDGE:-10}"
LIVE_SHORT_SEGMENTS="${LIVE_SHORT_SEGMENTS:-WD-02,WD-23}"
DAILY_PROFILE_SELECTOR="${DAILY_PROFILE_SELECTOR:-1}"
DAILY_PROFILE_LOOKBACK_DAYS="${DAILY_PROFILE_LOOKBACK_DAYS:-7}"
DAILY_PROFILE_STABLE_LOOKBACK_DAYS="${DAILY_PROFILE_STABLE_LOOKBACK_DAYS-}"
DAILY_PROFILE_MIN_SAMPLES="${DAILY_PROFILE_MIN_SAMPLES:-20}"
DAILY_PROFILE_WEEKEND_MIN_SAMPLES="${DAILY_PROFILE_WEEKEND_MIN_SAMPLES:-10}"
DAILY_PROFILE_MIN_WIN_RATE="${DAILY_PROFILE_MIN_WIN_RATE:-0.60}"
DAILY_PROFILE_MIN_EV="${DAILY_PROFILE_MIN_EV:-0}"
DAILY_PROFILE_EXIT_WIN_RATE="${DAILY_PROFILE_EXIT_WIN_RATE:-0.60}"
DAILY_PROFILE_EXIT_EV="${DAILY_PROFILE_EXIT_EV:-0}"
DAILY_PROFILE_DEGRADED_RUNS="${DAILY_PROFILE_DEGRADED_RUNS:-2}"
DAILY_PROFILE_JOINT_FAILURES_TO_EXIT="${DAILY_PROFILE_JOINT_FAILURES_TO_EXIT-}"
DAILY_PROFILE_MAX_ACTIVE="${DAILY_PROFILE_MAX_ACTIVE:-0}"
DAILY_PROFILE_EVALUATION_TIME="${DAILY_PROFILE_EVALUATION_TIME:-07:50}"
DAILY_PROFILE_ACTIVATION_TIME="${DAILY_PROFILE_ACTIVATION_TIME:-08:00}"
NO_WARMUP="${NO_WARMUP:-0}"
NO_PERSISTENCE="${NO_PERSISTENCE:-0}"
NO_WEBHOOK="${NO_WEBHOOK:-0}"
NO_WEBSOCKET="${NO_WEBSOCKET:-0}"
WARMUP_CURRENT_MONTH_DAILY="${WARMUP_CURRENT_MONTH_DAILY:-1}"

usage() {
  cat <<'USAGE'
用法: scripts/run.sh [SYMBOL] [PORT]
      scripts/run.sh [参数]

启动币安事件合约量价监控程序。

参数:
  --symbol SYMBOL        交易对，默认: BTCUSDT
  --strategy-build-id ID 策略构建标识；未指定时由程序按策略源码生成
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
  --trade-score-threshold N|auto
                         兼容审计参数，不改变开单资格，默认: auto
  --win-return N         赢单返还金额，默认: stake * 1.8
  --max-open-orders N    最多同时持有的未结订单数，默认: 2
  --max-open-long-orders N
                         最多同时持有的 LONG 未结订单数，默认: 1
  --max-open-short-orders N
                         最多同时持有的 SHORT 未结订单数，默认: 2
  --min-order-gap-minutes N
                         同方向两次开单最小间隔分钟数，默认: 2
  --no-stake-progression 关闭两阶段金额叠加
  --no-rolling-edge-guard
                         关闭滚动优势守卫，仅保留状态观察
  --no-result-sequence-guard
                         关闭同方向连续亏损冷却守卫，默认启用
  --no-time-period-guard 关闭北京时间12:00-18:00真实开单暂停，默认已关闭
  --no-profile-health-guard
                         关闭24小时画像健康守卫，默认每4小时按方向评估一次
  --result-sequence-loss-streak N
                         同方向连续已结算亏损触发笔数，默认: 3
  --result-sequence-cooldown-minutes N
                         触发后的冷却分钟数，默认: 20
  --result-sequence-scope SCOPE
                         统计范围 GLOBAL 或 DIRECTION，默认: DIRECTION
  --stake-progression-max-orders N
                         兼容参数；两阶段固定为 2 级，默认: 2
  --stake-progression-max-active N
                         每个方向最多并行第二级订单数，默认: 1
  --stake-progression-base-only-segments LIST
                         兼容参数；仅使用基础金额、不继承第二级金额的时段，逗号分隔
                         默认空，生产默认所有已入选时段均可参与
  --profile-guard        开启画像守卫正式拦截，默认只做影子观察
  --profile-guard-min-history N
                         画像守卫启用前需要的历史订单数量，默认: 15
  --profile-guard-min-group-size N
                         画像守卫单个弱点最小历史样本数，默认: 2
  --profile-degradation-cooldown-minutes N
                         完整画像连续亏损3单后的冷却分钟数，0关闭，默认: 60
  --no-observation-profile-promotion
                         关闭已结算观察画像对静态时段拦截的动态放行
  --observation-profile-lookback-days N
                         观察画像滚动统计天数，默认: 7
  --observation-profile-min-samples N
                         观察画像最小独立已结算样本数，默认: 12
  --observation-profile-min-win-rate N
                         观察画像最低胜率，默认: 0.72
  --observation-profile-min-ev N
                         观察画像最低单笔期望收益，默认: 4U
  --observation-profile-min-edge N
                         观察画像动态放行最低评分边际，默认: 10
  --live-short-segments LIST
                         允许实际开 SHORT 的时段，逗号分隔，默认: WD-02,WD-23
  --no-daily-profile-selector
                         关闭每日观察画像策略选择器，回退到静态主策略
  --daily-profile-lookback-days N
                         每日画像统计回看天数，默认: 7
  --daily-profile-stable-lookback-days N
                         稳定窗口天数，未指定时取 14 与快速窗口天数的较大值
  --daily-profile-min-samples N
                         工作日新画像入选所需最小独立样本数，默认: 20
  --daily-profile-weekend-min-samples N
                         周末新画像入选所需最小独立样本数，默认: 10
  --daily-profile-min-win-rate N
                         新画像入选最低胜率，默认: 0.60
  --daily-profile-min-ev N
                         新画像入选最低单笔期望收益，默认: 0U
  --daily-profile-exit-win-rate N
                         已启用画像退化胜率线，默认: 0.60
  --daily-profile-exit-ev N
                         已启用画像退化EV线，默认: 0U
  --daily-profile-degraded-runs N
                         兼容连续退化次数，默认: 2
  --daily-profile-joint-failures-to-exit N
                         双窗口同时失败多少次后退出，未指定时沿用连续退化次数
  --daily-profile-max-active N
                         每天最多启用画像数量，0 表示不限制，默认: 0
  --daily-profile-evaluation-time HH:MM
                         每天北京时间画像评估时间，默认: 07:50
  --daily-profile-activation-time HH:MM
                         每天北京时间画像生效时间，默认: 08:00
  --no-current-month-daily
                         跳过当前月份日线历史预热
  --no-warmup            关闭本地/远程历史预热
  --no-persistence       关闭 SQLite 订单和信号持久化
  --no-webhook           关闭外部 webhook 信号推送
  --no-websocket         关闭 WebSocket 实时行情，改用 REST 轮询兜底
  --python PATH          Python 可执行文件，默认: python3 或 PYTHON_BIN
  -h, --help             显示帮助并退出

环境变量覆盖:
  SYMBOL, STRATEGY_BUILD_ID, HOST, PORT, POLL_SECONDS, KLINE_LIMIT, DATA_DIR, DB_PATH,
  WEBHOOK_URL, WEBHOOK_TOKEN, WEBHOOK_TIMEOUT,
  WARMUP_MONTHS, WARMUP_TIMEOUT, STAKE, TRADE_SCORE_THRESHOLD, WIN_RETURN,
  MAX_OPEN_ORDERS, MAX_OPEN_LONG_ORDERS, MAX_OPEN_SHORT_ORDERS,
  MIN_ORDER_GAP_MINUTES,
  STAKE_PROGRESSION, ROLLING_EDGE_GUARD, STAKE_PROGRESSION_MAX_ORDERS,
  RESULT_SEQUENCE_GUARD, RESULT_SEQUENCE_LOSS_STREAK,
  RESULT_SEQUENCE_COOLDOWN_MINUTES, RESULT_SEQUENCE_SCOPE,
  TIME_PERIOD_GUARD, PROFILE_HEALTH_GUARD,
  STAKE_PROGRESSION_MAX_ACTIVE, STAKE_PROGRESSION_BASE_ONLY_SEGMENTS,
  PROFILE_GUARD, PROFILE_GUARD_MIN_HISTORY, PROFILE_GUARD_MIN_GROUP_SIZE,
  PROFILE_DEGRADATION_COOLDOWN_MINUTES,
  OBSERVATION_PROFILE_PROMOTION, OBSERVATION_PROFILE_LOOKBACK_DAYS,
  OBSERVATION_PROFILE_MIN_SAMPLES, OBSERVATION_PROFILE_MIN_WIN_RATE,
  OBSERVATION_PROFILE_MIN_EV, OBSERVATION_PROFILE_MIN_EDGE, LIVE_SHORT_SEGMENTS,
  DAILY_PROFILE_SELECTOR, DAILY_PROFILE_LOOKBACK_DAYS,
  DAILY_PROFILE_STABLE_LOOKBACK_DAYS,
  DAILY_PROFILE_MIN_SAMPLES, DAILY_PROFILE_WEEKEND_MIN_SAMPLES,
  DAILY_PROFILE_MIN_WIN_RATE, DAILY_PROFILE_MIN_EV,
  DAILY_PROFILE_EXIT_WIN_RATE, DAILY_PROFILE_EXIT_EV,
  DAILY_PROFILE_DEGRADED_RUNS, DAILY_PROFILE_JOINT_FAILURES_TO_EXIT,
  DAILY_PROFILE_MAX_ACTIVE,
  DAILY_PROFILE_EVALUATION_TIME, DAILY_PROFILE_ACTIVATION_TIME,
  NO_WARMUP, NO_PERSISTENCE, NO_WEBHOOK, NO_WEBSOCKET,
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
    --strategy-build-id)
      require_value "$1" "${2:-}"
      STRATEGY_BUILD_ID="$2"
      shift 2
      ;;
    --strategy-build-id=*)
      STRATEGY_BUILD_ID="${1#*=}"
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
    --trade-score-threshold)
      require_value "$1" "${2:-}"
      TRADE_SCORE_THRESHOLD="$2"
      shift 2
      ;;
    --trade-score-threshold=*)
      TRADE_SCORE_THRESHOLD="${1#*=}"
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
    --max-open-orders)
      require_value "$1" "${2:-}"
      MAX_OPEN_ORDERS="$2"
      shift 2
      ;;
    --max-open-orders=*)
      MAX_OPEN_ORDERS="${1#*=}"
      shift
      ;;
    --max-open-long-orders)
      require_value "$1" "${2:-}"
      MAX_OPEN_LONG_ORDERS="$2"
      shift 2
      ;;
    --max-open-long-orders=*)
      MAX_OPEN_LONG_ORDERS="${1#*=}"
      shift
      ;;
    --max-open-short-orders)
      require_value "$1" "${2:-}"
      MAX_OPEN_SHORT_ORDERS="$2"
      shift 2
      ;;
    --max-open-short-orders=*)
      MAX_OPEN_SHORT_ORDERS="${1#*=}"
      shift
      ;;
    --min-order-gap-minutes)
      require_value "$1" "${2:-}"
      MIN_ORDER_GAP_MINUTES="$2"
      shift 2
      ;;
    --min-order-gap-minutes=*)
      MIN_ORDER_GAP_MINUTES="${1#*=}"
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
    --stake-progression-max-active)
      require_value "$1" "${2:-}"
      STAKE_PROGRESSION_MAX_ACTIVE="$2"
      shift 2
      ;;
    --stake-progression-max-active=*)
      STAKE_PROGRESSION_MAX_ACTIVE="${1#*=}"
      shift
      ;;
    --stake-progression-base-only-segments)
      if [ "$#" -lt 2 ]; then
        echo "参数 $1 缺少值" >&2
        exit 2
      fi
      STAKE_PROGRESSION_BASE_ONLY_SEGMENTS="$2"
      shift 2
      ;;
    --stake-progression-base-only-segments=*)
      STAKE_PROGRESSION_BASE_ONLY_SEGMENTS="${1#*=}"
      shift
      ;;
    --no-stake-progression)
      STAKE_PROGRESSION="0"
      shift
      ;;
    --no-rolling-edge-guard)
      ROLLING_EDGE_GUARD="0"
      shift
      ;;
    --no-result-sequence-guard)
      RESULT_SEQUENCE_GUARD="0"
      shift
      ;;
    --no-time-period-guard)
      TIME_PERIOD_GUARD="0"
      shift
      ;;
    --no-profile-health-guard)
      PROFILE_HEALTH_GUARD="0"
      shift
      ;;
    --result-sequence-loss-streak)
      require_value "$1" "${2:-}"
      RESULT_SEQUENCE_LOSS_STREAK="$2"
      shift 2
      ;;
    --result-sequence-loss-streak=*)
      RESULT_SEQUENCE_LOSS_STREAK="${1#*=}"
      shift
      ;;
    --result-sequence-cooldown-minutes)
      require_value "$1" "${2:-}"
      RESULT_SEQUENCE_COOLDOWN_MINUTES="$2"
      shift 2
      ;;
    --result-sequence-cooldown-minutes=*)
      RESULT_SEQUENCE_COOLDOWN_MINUTES="${1#*=}"
      shift
      ;;
    --result-sequence-scope)
      require_value "$1" "${2:-}"
      RESULT_SEQUENCE_SCOPE="$2"
      shift 2
      ;;
    --result-sequence-scope=*)
      RESULT_SEQUENCE_SCOPE="${1#*=}"
      shift
      ;;
    --profile-guard)
      PROFILE_GUARD="1"
      shift
      ;;
    --profile-guard-min-history)
      require_value "$1" "${2:-}"
      PROFILE_GUARD_MIN_HISTORY="$2"
      shift 2
      ;;
    --profile-guard-min-history=*)
      PROFILE_GUARD_MIN_HISTORY="${1#*=}"
      shift
      ;;
    --profile-guard-min-group-size)
      require_value "$1" "${2:-}"
      PROFILE_GUARD_MIN_GROUP_SIZE="$2"
      shift 2
      ;;
    --profile-guard-min-group-size=*)
      PROFILE_GUARD_MIN_GROUP_SIZE="${1#*=}"
      shift
      ;;
    --profile-degradation-cooldown-minutes)
      require_value "$1" "${2:-}"
      PROFILE_DEGRADATION_COOLDOWN_MINUTES="$2"
      shift 2
      ;;
    --profile-degradation-cooldown-minutes=*)
      PROFILE_DEGRADATION_COOLDOWN_MINUTES="${1#*=}"
      shift
      ;;
    --no-observation-profile-promotion)
      OBSERVATION_PROFILE_PROMOTION="0"
      shift
      ;;
    --observation-profile-lookback-days)
      require_value "$1" "${2:-}"
      OBSERVATION_PROFILE_LOOKBACK_DAYS="$2"
      shift 2
      ;;
    --observation-profile-lookback-days=*)
      OBSERVATION_PROFILE_LOOKBACK_DAYS="${1#*=}"
      shift
      ;;
    --observation-profile-min-samples)
      require_value "$1" "${2:-}"
      OBSERVATION_PROFILE_MIN_SAMPLES="$2"
      shift 2
      ;;
    --observation-profile-min-samples=*)
      OBSERVATION_PROFILE_MIN_SAMPLES="${1#*=}"
      shift
      ;;
    --observation-profile-min-win-rate)
      require_value "$1" "${2:-}"
      OBSERVATION_PROFILE_MIN_WIN_RATE="$2"
      shift 2
      ;;
    --observation-profile-min-win-rate=*)
      OBSERVATION_PROFILE_MIN_WIN_RATE="${1#*=}"
      shift
      ;;
    --observation-profile-min-ev)
      require_value "$1" "${2:-}"
      OBSERVATION_PROFILE_MIN_EV="$2"
      shift 2
      ;;
    --observation-profile-min-ev=*)
      OBSERVATION_PROFILE_MIN_EV="${1#*=}"
      shift
      ;;
    --observation-profile-min-edge)
      require_value "$1" "${2:-}"
      OBSERVATION_PROFILE_MIN_EDGE="$2"
      shift 2
      ;;
    --observation-profile-min-edge=*)
      OBSERVATION_PROFILE_MIN_EDGE="${1#*=}"
      shift
      ;;
    --live-short-segments)
      require_value "$1" "${2:-}"
      LIVE_SHORT_SEGMENTS="$2"
      shift 2
      ;;
    --live-short-segments=*)
      LIVE_SHORT_SEGMENTS="${1#*=}"
      shift
      ;;
    --no-daily-profile-selector)
      DAILY_PROFILE_SELECTOR="0"
      shift
      ;;
    --daily-profile-lookback-days)
      require_value "$1" "${2:-}"
      DAILY_PROFILE_LOOKBACK_DAYS="$2"
      shift 2
      ;;
    --daily-profile-lookback-days=*)
      DAILY_PROFILE_LOOKBACK_DAYS="${1#*=}"
      shift
      ;;
    --daily-profile-stable-lookback-days)
      require_value "$1" "${2:-}"
      DAILY_PROFILE_STABLE_LOOKBACK_DAYS="$2"
      shift 2
      ;;
    --daily-profile-stable-lookback-days=*)
      DAILY_PROFILE_STABLE_LOOKBACK_DAYS="${1#*=}"
      shift
      ;;
    --daily-profile-min-samples)
      require_value "$1" "${2:-}"
      DAILY_PROFILE_MIN_SAMPLES="$2"
      shift 2
      ;;
    --daily-profile-min-samples=*)
      DAILY_PROFILE_MIN_SAMPLES="${1#*=}"
      shift
      ;;
    --daily-profile-weekend-min-samples)
      require_value "$1" "${2:-}"
      DAILY_PROFILE_WEEKEND_MIN_SAMPLES="$2"
      shift 2
      ;;
    --daily-profile-weekend-min-samples=*)
      DAILY_PROFILE_WEEKEND_MIN_SAMPLES="${1#*=}"
      shift
      ;;
    --daily-profile-min-win-rate)
      require_value "$1" "${2:-}"
      DAILY_PROFILE_MIN_WIN_RATE="$2"
      shift 2
      ;;
    --daily-profile-min-win-rate=*)
      DAILY_PROFILE_MIN_WIN_RATE="${1#*=}"
      shift
      ;;
    --daily-profile-min-ev)
      require_value "$1" "${2:-}"
      DAILY_PROFILE_MIN_EV="$2"
      shift 2
      ;;
    --daily-profile-min-ev=*)
      DAILY_PROFILE_MIN_EV="${1#*=}"
      shift
      ;;
    --daily-profile-exit-win-rate)
      require_value "$1" "${2:-}"
      DAILY_PROFILE_EXIT_WIN_RATE="$2"
      shift 2
      ;;
    --daily-profile-exit-win-rate=*)
      DAILY_PROFILE_EXIT_WIN_RATE="${1#*=}"
      shift
      ;;
    --daily-profile-exit-ev)
      require_value "$1" "${2:-}"
      DAILY_PROFILE_EXIT_EV="$2"
      shift 2
      ;;
    --daily-profile-exit-ev=*)
      DAILY_PROFILE_EXIT_EV="${1#*=}"
      shift
      ;;
    --daily-profile-degraded-runs)
      require_value "$1" "${2:-}"
      DAILY_PROFILE_DEGRADED_RUNS="$2"
      shift 2
      ;;
    --daily-profile-degraded-runs=*)
      DAILY_PROFILE_DEGRADED_RUNS="${1#*=}"
      shift
      ;;
    --daily-profile-joint-failures-to-exit)
      require_value "$1" "${2:-}"
      DAILY_PROFILE_JOINT_FAILURES_TO_EXIT="$2"
      shift 2
      ;;
    --daily-profile-joint-failures-to-exit=*)
      DAILY_PROFILE_JOINT_FAILURES_TO_EXIT="${1#*=}"
      shift
      ;;
    --daily-profile-max-active)
      require_value "$1" "${2:-}"
      DAILY_PROFILE_MAX_ACTIVE="$2"
      shift 2
      ;;
    --daily-profile-max-active=*)
      DAILY_PROFILE_MAX_ACTIVE="${1#*=}"
      shift
      ;;
    --daily-profile-evaluation-time)
      require_value "$1" "${2:-}"
      DAILY_PROFILE_EVALUATION_TIME="$2"
      shift 2
      ;;
    --daily-profile-evaluation-time=*)
      DAILY_PROFILE_EVALUATION_TIME="${1#*=}"
      shift
      ;;
    --daily-profile-activation-time)
      require_value "$1" "${2:-}"
      DAILY_PROFILE_ACTIVATION_TIME="$2"
      shift 2
      ;;
    --daily-profile-activation-time=*)
      DAILY_PROFILE_ACTIVATION_TIME="${1#*=}"
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
    --no-websocket)
      NO_WEBSOCKET="1"
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
case "$NO_WEBSOCKET" in
  1|true|TRUE|yes|YES|y|Y|on|ON)
    EXTRA_ARGS+=(--no-websocket)
    ;;
esac
case "$STAKE_PROGRESSION" in
  0|false|FALSE|no|NO|n|N|off|OFF)
    EXTRA_ARGS+=(--no-stake-progression)
    ;;
esac
case "$ROLLING_EDGE_GUARD" in
  0|false|FALSE|no|NO|n|N|off|OFF)
    EXTRA_ARGS+=(--no-rolling-edge-guard)
    ;;
esac
case "$RESULT_SEQUENCE_GUARD" in
  0|false|FALSE|no|NO|n|N|off|OFF)
    EXTRA_ARGS+=(--no-result-sequence-guard)
    ;;
esac
case "$TIME_PERIOD_GUARD" in
  0|false|FALSE|no|NO|n|N|off|OFF)
    EXTRA_ARGS+=(--no-time-period-guard)
    ;;
esac
case "$PROFILE_HEALTH_GUARD" in
  0|false|FALSE|no|NO|n|N|off|OFF)
    EXTRA_ARGS+=(--no-profile-health-guard)
    ;;
esac
case "$PROFILE_GUARD" in
  1|true|TRUE|yes|YES|y|Y|on|ON)
    EXTRA_ARGS+=(--profile-guard)
    ;;
esac
case "$OBSERVATION_PROFILE_PROMOTION" in
  0|false|FALSE|no|NO|n|N|off|OFF)
    EXTRA_ARGS+=(--no-observation-profile-promotion)
    ;;
esac
case "$DAILY_PROFILE_SELECTOR" in
  0|false|FALSE|no|NO|n|N|off|OFF)
    EXTRA_ARGS+=(--no-daily-profile-selector)
    ;;
esac
case "$WARMUP_CURRENT_MONTH_DAILY" in
  0|false|FALSE|no|NO|n|N|off|OFF)
    EXTRA_ARGS+=(--no-current-month-daily)
    ;;
esac
if [ -n "$DAILY_PROFILE_STABLE_LOOKBACK_DAYS" ]; then
  EXTRA_ARGS+=(--daily-profile-stable-lookback-days "$DAILY_PROFILE_STABLE_LOOKBACK_DAYS")
fi
if [ -n "$DAILY_PROFILE_JOINT_FAILURES_TO_EXIT" ]; then
  EXTRA_ARGS+=(--daily-profile-joint-failures-to-exit "$DAILY_PROFILE_JOINT_FAILURES_TO_EXIT")
fi
if [ -n "$STRATEGY_BUILD_ID" ]; then
  EXTRA_ARGS+=(--strategy-build-id "$STRATEGY_BUILD_ID")
fi

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
  --trade-score-threshold "$TRADE_SCORE_THRESHOLD" \
  ${WIN_RETURN:+--win-return "$WIN_RETURN"} \
  --max-open-orders "$MAX_OPEN_ORDERS" \
  --max-open-long-orders "$MAX_OPEN_LONG_ORDERS" \
  --max-open-short-orders "$MAX_OPEN_SHORT_ORDERS" \
  --min-order-gap-minutes "$MIN_ORDER_GAP_MINUTES" \
  --result-sequence-loss-streak "$RESULT_SEQUENCE_LOSS_STREAK" \
  --result-sequence-cooldown-minutes "$RESULT_SEQUENCE_COOLDOWN_MINUTES" \
  --result-sequence-scope "$RESULT_SEQUENCE_SCOPE" \
  --stake-progression-max-orders "$STAKE_PROGRESSION_MAX_ORDERS" \
  --stake-progression-max-active "$STAKE_PROGRESSION_MAX_ACTIVE" \
  --stake-progression-base-only-segments "$STAKE_PROGRESSION_BASE_ONLY_SEGMENTS" \
  --profile-guard-min-history "$PROFILE_GUARD_MIN_HISTORY" \
  --profile-guard-min-group-size "$PROFILE_GUARD_MIN_GROUP_SIZE" \
  --profile-degradation-cooldown-minutes "$PROFILE_DEGRADATION_COOLDOWN_MINUTES" \
  --observation-profile-lookback-days "$OBSERVATION_PROFILE_LOOKBACK_DAYS" \
  --observation-profile-min-samples "$OBSERVATION_PROFILE_MIN_SAMPLES" \
  --observation-profile-min-win-rate "$OBSERVATION_PROFILE_MIN_WIN_RATE" \
  --observation-profile-min-ev "$OBSERVATION_PROFILE_MIN_EV" \
  --observation-profile-min-edge "$OBSERVATION_PROFILE_MIN_EDGE" \
  --live-short-segments "$LIVE_SHORT_SEGMENTS" \
  --daily-profile-lookback-days "$DAILY_PROFILE_LOOKBACK_DAYS" \
  --daily-profile-min-samples "$DAILY_PROFILE_MIN_SAMPLES" \
  --daily-profile-weekend-min-samples "$DAILY_PROFILE_WEEKEND_MIN_SAMPLES" \
  --daily-profile-min-win-rate "$DAILY_PROFILE_MIN_WIN_RATE" \
  --daily-profile-min-ev "$DAILY_PROFILE_MIN_EV" \
  --daily-profile-exit-win-rate "$DAILY_PROFILE_EXIT_WIN_RATE" \
  --daily-profile-exit-ev "$DAILY_PROFILE_EXIT_EV" \
  --daily-profile-degraded-runs "$DAILY_PROFILE_DEGRADED_RUNS" \
  --daily-profile-max-active "$DAILY_PROFILE_MAX_ACTIVE" \
  --daily-profile-evaluation-time "$DAILY_PROFILE_EVALUATION_TIME" \
  --daily-profile-activation-time "$DAILY_PROFILE_ACTIVATION_TIME" \
  ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
