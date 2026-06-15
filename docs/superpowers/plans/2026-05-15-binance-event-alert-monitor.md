# Binance Event Alert Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python monitor that reads Binance spot 1m klines, generates 10m/30m volume-price event alerts, and simulates fixed-size event-contract orders.

**Architecture:** The app is split into small modules: market data fetching, signal generation, simulated order settlement, in-memory state, and a FastAPI dashboard/API. Core trading behavior is covered by tests and does not depend on network access.

**Tech Stack:** Python 3.10+ standard library HTTP server, urllib, unittest, plain HTML/CSS/JavaScript.

---

### Task 1: Core domain models and signal rules

**Files:**
- Create: `app/models.py`
- Create: `app/strategy.py`
- Test: `tests/test_strategy.py`

- [ ] Define Kline, Signal, and direction/timeframe constants.
- [ ] Add tests for high-volume low-position long, high-position volume-stall short, and no-trade wait cases.
- [ ] Implement deterministic volume-price classification with rolling price position and volume ratio.

### Task 2: Simulated event-contract order engine

**Files:**
- Create: `app/simulator.py`
- Test: `tests/test_simulator.py`

- [ ] Add tests for LONG win/loss and SHORT win/loss.
- [ ] Implement fixed stake order creation at 10U.
- [ ] Implement settlement: win net +8U, loss net -10U, with entry/expiry prices recorded.

### Task 3: Binance kline client

**Files:**
- Create: `app/binance_client.py`
- Test: `tests/test_binance_client.py`

- [ ] Add parser test for Binance REST kline array shape.
- [ ] Implement `/api/v3/klines` fetch with `symbol`, `interval=1m`, and `limit`.
- [ ] Keep network code injectable so tests can use fake responses.

### Task 4: Runtime service and dashboard

**Files:**
- Create: `app/state.py`
- Create: `app/server.py`
- Create: `app/static/index.html`
- Create: `app/static/styles.css`
- Create: `app/static/app.js`

- [ ] Implement background polling loop.
- [ ] Generate alerts from the latest klines.
- [ ] Open simulated 10m/30m orders when new actionable alerts appear.
- [ ] Settle expired orders.
- [ ] Expose JSON endpoints and a standalone monitoring page through the standard-library HTTP server.

### Task 5: Run and verify

**Files:**
- Create: `README.md`

- [ ] Run `python3 -m unittest discover -s tests`.
- [ ] Start with `python3 -m app.server`.
- [ ] Open `http://127.0.0.1:8000`.
- [ ] Verify current price, latest alert, open/settled orders, win rate, and PnL are visible.
