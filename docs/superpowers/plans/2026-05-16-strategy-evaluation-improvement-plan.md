# Strategy Evaluation Improvement Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the BTC event-contract strategy evaluation match live warmup behavior and reduce overfit risk before running long-lived simulation.

**Architecture:** Split evaluation into a fast replay engine, walk-forward profile generation, and report/audit outputs. Keep live strategy rules in `app/strategy.py`; move expensive repeated indicator/profile work into cached backtest helpers so live behavior and backtest behavior can be compared without waiting minutes per month.

**Tech Stack:** Python standard library, existing Binance Vision ZIP data, existing `app.models`, `app.backtest`, `app.strategy`, `app.history`.

---

## Current evidence

- Exact rerun on `data/BTCUSDT-1m-2026-04.zip`: `97` orders, `69.07%` win rate, `+236U`.
- Existing final reports for `2026-02` to `2026-04`: `347` orders, `68.59%` win rate, `+814U`.
- Risk from exact `2026-02` to `2026-04` reports: max drawdown `-74U`, max loss streak `6`.
- Quick 10-minute-step replay across `2025-12` to `2026-04`: `207` orders, `57.49%` win rate, `+72U`. This is not an exact production replay, but it shows weaker Dec/Jan and 30m fragility.
- MCP current context on 2026-05-16: Fear & Greed `31 Fear`, 90-day average `21.3`, trend rising; 30m RSI around `41`, MACD strategy recently `BUY`, Bollinger strategy mostly `HOLD`.

## Task 1: Backtest engine performance and live-aligned warmup

**Files:**
- Modify: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/app/backtest.py`
- Test: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/tests/test_backtest.py`

- [ ] Add a test that combined multi-month replay can use a 30-day strategy history without changing order settlement semantics.
- [ ] Add a helper that loads multiple monthly ZIPs, deduplicates by `open_time`, and sorts ascending.
- [ ] Add a replay mode that reports elapsed time and supports `strategy_history_limit=43200`.
- [ ] Cache rolling technical/profile inputs so one month does not take about five minutes to replay.
- [ ] Verify exact April stats remain `97` orders, `67` wins, `30` losses, `+236U`.

## Task 2: Walk-forward session edge generation

**Files:**
- Create: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/app/session_profiles.py`
- Modify: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/app/strategy.py`
- Test: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/tests/test_strategy.py`

- [ ] Generate `SESSION_EDGE_BY_TIMEFRAME` from prior data only, not from the same month being evaluated.
- [ ] Use Dec/Jan as out-of-sample validation for the existing Feb-Apr-derived segments.
- [ ] Require a segment to pass minimum sample size, win rate, and EV in at least two independent months before it can open orders.
- [ ] Downgrade `30|WD-15` unless it recovers after walk-forward validation; exact Feb-Apr stats are only `59.18%`, `+32U`, low margin.

## Task 3: Regime-aware risk filter

**Files:**
- Modify: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/app/strategy.py`
- Test: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/tests/test_strategy.py`

- [ ] Add a regime label from rolling 30-day volatility, Bollinger width percentile, Fear & Greed level/trend, and 30m MACD direction.
- [ ] In fearful-but-rising regimes, keep rebound LONG enabled but raise chase-LONG threshold after upper-band approaches.
- [ ] In fear-falling regimes, require extra confirmation for SHORT and disable low-margin 30m LONG sessions.
- [ ] Add tests for: fear rising, fear falling, high BB width, compressed BB width.

## Task 4: Direction and duration controls

**Files:**
- Modify: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/app/strategy.py`
- Modify: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/app/state.py`
- Test: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/tests/test_state.py`

- [ ] Keep SHORT available, but require both bearish indicator confirmation and prior-month positive segment edge.
- [ ] Prefer 10m over 30m when both are actionable unless the 30m segment has materially higher EV and sample size.
- [ ] Add a rolling daily loss guard: after three consecutive losses or drawdown below `-40U`, pause opening for that UTC hour segment.
- [ ] Add tests for 10m-vs-30m ranking, SHORT gating, and loss-guard pause.

## Task 5: Report and dashboard audit fields

**Files:**
- Modify: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/app/backtest.py`
- Modify: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/app/static/app.js`
- Modify: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/app/static/index.html`
- Test: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/tests/test_backtest.py`

- [ ] Add report fields: max drawdown, max loss streak, by-month stats, by-regime stats, rejected-actionable counts.
- [ ] Show live regime, warmup range, and current risk pause status on the monitor page.
- [ ] Store every blocked signal reason so it is possible to audit whether filters are too strict.

