# Dynamic F&G and Indicator Thresholds Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add dynamic Fear & Greed risk adjustment and per-session MACD/RSI/BOLL indicator thresholds to the Binance event-contract alert monitor.

**Architecture:** Keep the strategy deterministic and testable by making dynamic thresholds local-data derived. Fetch Fear & Greed in the monitor layer with caching/fallback, then pass it into strategy analysis as a small context object that only adjusts risk thresholds, not raw direction. Build indicator profiles from existing rolling history per `timeframe + direction + WD/WE + UTC hour` and fall back to global profiles when sample size is low.

**Tech Stack:** Python standard library, `unittest`, existing Binance Kline models/backtester/server, existing static HTML/CSS/JS.

---

### Task 1: Add Fear & Greed risk context model and tests

**Files:**
- Modify: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/app/models.py`
- Modify: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/app/strategy.py`
- Test: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/tests/test_strategy.py`

- [ ] Add a `FearGreedContext` dataclass with `value`, `classification`, `average_30d`, `trend`, and `updated_at_ms`.
- [ ] Write tests proving fear raises SHORT threshold and greed raises LONG threshold.
- [ ] Run the focused tests and confirm they fail because the context is not implemented.
- [ ] Implement minimal context plumbing into `analyze_volume_price`.
- [ ] Re-run focused tests and full suite.

### Task 2: Add dynamic indicator profile thresholds

**Files:**
- Modify: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/app/strategy.py`
- Test: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/tests/test_strategy.py`

- [ ] Add tests for session-specific RSI/BOLL/MACD threshold adaptation.
- [ ] Run focused tests and confirm they fail under fixed thresholds.
- [ ] Implement `IndicatorProfile` from local rolling history using per-session and fallback global samples.
- [ ] Replace fixed `RSI 35~70` and `BOLL 0.35~0.85` checks with profile-derived bounds.
- [ ] Expose profile fields on `Signal`.
- [ ] Re-run focused tests and full suite.

### Task 3: Fetch and cache Fear & Greed in monitor state

**Files:**
- Create: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/app/fear_greed.py`
- Modify: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/app/state.py`
- Modify: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/app/server.py`
- Test: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/tests/test_state.py`

- [ ] Write tests using an injected provider to prove cached F&G is passed into strategy and snapshot.
- [ ] Run focused tests and confirm they fail.
- [ ] Implement provider with Alternative.me API JSON parsing, timeout, TTL, and stale fallback.
- [ ] Inject provider into `MonitorState`.
- [ ] Re-run focused tests and full suite.

### Task 4: Display dynamic risk and profile fields

**Files:**
- Modify: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/app/static/index.html`
- Modify: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/app/static/app.js`
- Modify: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/app/static/styles.css`

- [ ] Show F&G value/classification/trend in summary cards.
- [ ] Show dynamic RSI/BOLL/MACD bounds and sample size in selected signal metrics.
- [ ] Browser-check the local page renders the new fields.

### Task 5: Backtest and MCP review

**Files:**
- Modify: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/README.md`
- Generate: `/Users/pengyong/Documents/Codex/2026-05-15/1-2-3-4-k-10/reports/*dynamic_fng_indicators*.json`

- [ ] Run February/March/April 2026 BTCUSDT 1m backtests.
- [ ] Call `crypto-feargreed-mcp` and `crypto-indicators-mcp` for current context review.
- [ ] Update README with the actual verified result and limitations.
- [ ] Run full unittest suite and report exact evidence.
