# Two-Stage Stake Progression Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the ambiguous global three-order progression with a persistent two-stage `10U -> 18U` credit model whose simultaneous second-stage order count defaults to one and never exceeds the total open-order limit.

**Architecture:** Add a small domain ledger that owns pending credits and active second-stage capacity. `AccountSimulator` and walk-forward replay both call this ledger, while `MonitorState` persists each credit creation or consumption in the same SQLite transaction as the corresponding order change. Daily profile selection and order qualification remain unchanged; all selected time segments can use progression.

**Tech Stack:** Python 3.10+ standard library, `dataclasses`, SQLite, `unittest`, shell launch scripts, vanilla HTML/CSS/JavaScript.

---

### Task 1: Build the two-stage progression ledger

**Files:**
- Create: `app/stake_progression.py`
- Create: `tests/test_stake_progression.py`

- [ ] **Step 1: Write failing tests for credit creation, consumption, capacity, and cancellation**

Create `tests/test_stake_progression.py` with deterministic order IDs and timestamps:

```python
import unittest

from app.stake_progression import StakeProgressionCredit, TwoStageStakeProgression


class TwoStageStakeProgressionTest(unittest.TestCase):
    def ledger(self, *, max_active=1, enabled=True, credits=(), active_second_orders=0):
        return TwoStageStakeProgression(
            base_stake=10.0,
            base_win_return=18.0,
            enabled=enabled,
            max_active=max_active,
            max_open_orders=5,
            activated_at=1_000,
            credits=credits,
            active_second_orders=active_second_orders,
        )

    def test_first_stage_win_creates_one_credit_and_next_order_consumes_it(self):
        ledger = self.ledger()
        credit = ledger.settle(1, order_opened_at=1_000, step=1, result="WIN", settled_at=601_000)
        terms, consumed = ledger.assign(order_id=2, opened_at=602_000)

        self.assertEqual(credit.source_order_id, 1)
        self.assertEqual((terms.stake, terms.win_return, terms.step), (18.0, 32.4, 2))
        self.assertEqual(terms.source_order_id, 1)
        self.assertEqual(consumed.status, "CONSUMED")
        self.assertEqual(ledger.status()["active_second_orders"], 1)

    def test_second_stage_settlement_ends_chain_even_when_it_wins(self):
        ledger = self.ledger()
        ledger.settle(1, 1_000, 1, "WIN", 601_000)
        ledger.assign(2, 602_000)

        created = ledger.settle(2, 602_000, 2, "WIN", 1_202_000)
        next_terms, consumed = ledger.assign(3, 1_203_000)

        self.assertIsNone(created)
        self.assertIsNone(consumed)
        self.assertEqual((next_terms.stake, next_terms.step), (10.0, 1))

    def test_capacity_full_does_not_queue_extra_credit(self):
        ledger = self.ledger(max_active=1, active_second_orders=1)

        credit = ledger.settle(3, 1_000, 1, "WIN", 601_000)

        self.assertIsNone(credit)
        self.assertEqual(ledger.status()["pending_credits"], 0)

    def test_restore_cancels_pending_credits_above_capacity(self):
        credits = [
            StakeProgressionCredit(source_order_id=1, created_at=601_000),
            StakeProgressionCredit(source_order_id=2, created_at=602_000),
        ]
        ledger = self.ledger(max_active=1, credits=credits, active_second_orders=1)

        self.assertEqual(ledger.status()["pending_credits"], 0)
        self.assertEqual([item.status for item in credits], ["CANCELLED", "CANCELLED"])

    def test_max_active_is_clamped_to_open_order_limit(self):
        ledger = self.ledger(max_active=99)

        self.assertEqual(ledger.status()["max_active"], 5)

    def test_disabled_ledger_cancels_pending_credits(self):
        pending = StakeProgressionCredit(source_order_id=1, created_at=601_000)
        ledger = self.ledger(enabled=False, credits=[pending])

        terms, consumed = ledger.assign(2, 602_000)

        self.assertEqual(pending.status, "CANCELLED")
        self.assertEqual((terms.stake, terms.step), (10.0, 1))
        self.assertIsNone(consumed)

    def test_orders_before_activation_do_not_create_credit(self):
        ledger = self.ledger()

        credit = ledger.settle(1, order_opened_at=999, step=1, result="WIN", settled_at=601_000)

        self.assertIsNone(credit)
```

- [ ] **Step 2: Run the ledger tests and verify the missing module failure**

Run:

```bash
python3 -m unittest tests.test_stake_progression
```

Expected: `ModuleNotFoundError: No module named 'app.stake_progression'`.

- [ ] **Step 3: Implement the focused domain ledger**

Create `app/stake_progression.py` with these public types and behavior:

```python
from dataclasses import dataclass
from typing import Iterable


TWO_STAGE_VERSION = "TWO_STAGE_V1"


@dataclass
class StakeProgressionCredit:
    source_order_id: int
    created_at: int
    status: str = "PENDING"
    consumed_order_id: int | None = None
    consumed_at: int | None = None
    version: str = TWO_STAGE_VERSION


@dataclass(frozen=True)
class OrderTerms:
    stake: float
    win_return: float
    step: int
    source_order_id: int | None = None


class TwoStageStakeProgression:
    def __init__(
        self,
        *,
        base_stake: float,
        base_win_return: float,
        enabled: bool,
        max_active: int,
        max_open_orders: int,
        activated_at: int,
        credits: Iterable[StakeProgressionCredit] = (),
        active_second_orders: int = 0,
    ):
        self.base_stake = float(base_stake)
        self.base_win_return = float(base_win_return)
        self.enabled = bool(enabled)
        self.max_active = min(max(1, int(max_active)), max(1, int(max_open_orders)))
        self.activated_at = int(activated_at)
        self.credits = list(credits)
        self.active_second_orders = max(0, int(active_second_orders))
        if not self.enabled:
            self.cancel_pending()
        else:
            available = max(0, self.max_active - self.active_second_orders)
            pending = sorted(
                (item for item in self.credits if item.status == "PENDING"),
                key=lambda item: (item.created_at, item.source_order_id),
            )
            for credit in pending[available:]:
                credit.status = "CANCELLED"

    def assign(self, order_id: int, opened_at: int) -> tuple[OrderTerms, StakeProgressionCredit | None]:
        credit = next((item for item in self.credits if item.status == "PENDING"), None)
        if not self.enabled or credit is None:
            return self._base_terms(), None
        credit.status = "CONSUMED"
        credit.consumed_order_id = int(order_id)
        credit.consumed_at = int(opened_at)
        self.active_second_orders += 1
        stake = self.base_win_return
        return OrderTerms(stake, self._return_for(stake), 2, credit.source_order_id), credit

    def settle(
        self,
        order_id: int,
        order_opened_at: int,
        step: int,
        result: str,
        settled_at: int,
    ) -> StakeProgressionCredit | None:
        if int(step) == 2:
            self.active_second_orders = max(0, self.active_second_orders - 1)
            return None
        if not self.enabled or order_opened_at < self.activated_at or result != "WIN":
            return None
        if self._reserved_count() >= self.max_active:
            return None
        existing = next((item for item in self.credits if item.source_order_id == order_id), None)
        if existing is not None:
            return existing
        credit = StakeProgressionCredit(source_order_id=int(order_id), created_at=int(settled_at))
        self.credits.append(credit)
        return credit

    def cancel_pending(self) -> list[StakeProgressionCredit]:
        cancelled = []
        for credit in self.credits:
            if credit.status == "PENDING":
                credit.status = "CANCELLED"
                cancelled.append(credit)
        return cancelled

    def status(self) -> dict:
        pending = sum(item.status == "PENDING" for item in self.credits)
        next_terms = self._second_terms() if self.enabled and pending else self._base_terms()
        return {
            "enabled": self.enabled,
            "version": TWO_STAGE_VERSION,
            "max_orders": 2,
            "max_active": self.max_active,
            "active_second_orders": self.active_second_orders,
            "pending_credits": pending,
            "next_stake": next_terms.stake,
            "next_step": next_terms.step,
        }

    def _reserved_count(self) -> int:
        return self.active_second_orders + sum(item.status == "PENDING" for item in self.credits)

    def _base_terms(self) -> OrderTerms:
        return OrderTerms(round(self.base_stake, 4), round(self.base_win_return, 4), 1)

    def _second_terms(self) -> OrderTerms:
        stake = self.base_win_return
        return OrderTerms(round(stake, 4), self._return_for(stake), 2)

    def _return_for(self, stake: float) -> float:
        if self.base_stake <= 0:
            return round(self.base_win_return, 4)
        return round(stake * (self.base_win_return / self.base_stake), 4)
```

- [ ] **Step 4: Run the ledger tests**

Run `python3 -m unittest tests.test_stake_progression`.

Expected: all 7 tests pass.

- [ ] **Step 5: Commit the ledger**

```bash
git add app/stake_progression.py tests/test_stake_progression.py
git commit -m "feat: add two-stage stake progression ledger"
```

### Task 2: Integrate the ledger with simulated orders

**Files:**
- Modify: `app/models.py:96-130`
- Modify: `app/simulator.py:7-166`
- Modify: `tests/test_simulator.py:94-161`

- [ ] **Step 1: Replace legacy three-stage tests with strict concurrent two-stage tests**

Add these assertions to `tests/test_simulator.py` and remove expectations for step 3 and fixed-base segment exclusions:

```python
def test_two_stage_progression_consumes_only_one_credit_while_orders_overlap(self):
    simulator = AccountSimulator(
        enable_stake_progression=True,
        stake_progression_max_active=1,
        max_open_orders=5,
        stake_progression_activated_at=0,
    )
    first = simulator.open_order(signal("LONG", timeframe_minutes=1), 100.0, 0)
    base_overlap = simulator.open_order(signal("LONG", timeframe_minutes=2), 100.0, 30_000)
    simulator.settle_expired_orders(60_000, 101.0)
    second = simulator.open_order(signal("LONG", timeframe_minutes=1), 101.0, 60_000)
    another_base = simulator.open_order(signal("LONG", timeframe_minutes=1), 101.0, 90_000)

    self.assertEqual([first.stake, base_overlap.stake, second.stake, another_base.stake], [10.0, 10.0, 18.0, 10.0])
    self.assertEqual([first.stake_progression_step, second.stake_progression_step], [1, 2])
    self.assertEqual(second.stake_progression_source_order_id, first.id)

def test_all_segments_can_consume_second_stage_credit(self):
    simulator = AccountSimulator(
        enable_stake_progression=True,
        stake_progression_max_active=1,
        max_open_orders=5,
        stake_progression_activated_at=0,
    )
    first = simulator.open_order(signal("LONG", timeframe_minutes=1, threshold_segment="WD-12"), 100.0, 0)
    simulator.settle_expired_orders(60_000, 101.0)
    short = simulator.open_order(signal("SHORT", timeframe_minutes=1, threshold_segment="WD-23"), 101.0, 60_000)

    self.assertEqual((short.stake, short.stake_progression_step), (18.0, 2))

def test_second_stage_win_does_not_create_step_three(self):
    simulator = AccountSimulator(
        enable_stake_progression=True,
        stake_progression_max_active=1,
        max_open_orders=5,
        stake_progression_activated_at=0,
    )
    first = simulator.open_order(signal("LONG", timeframe_minutes=1), 100.0, 0)
    simulator.settle_expired_orders(60_000, 101.0)
    second = simulator.open_order(signal("LONG", timeframe_minutes=1), 101.0, 60_000)
    simulator.settle_expired_orders(120_000, 102.0)
    third = simulator.open_order(signal("LONG", timeframe_minutes=1), 102.0, 120_000)

    self.assertEqual([first.stake, second.stake, third.stake], [10.0, 18.0, 10.0])
    self.assertEqual([first.pnl, second.pnl], [8.0, 14.4])
```

- [ ] **Step 2: Run simulator tests and verify old implementation fails**

Run `python3 -m unittest tests.test_simulator`.

Expected: failures show repeated 18U/32.4U legacy progression and missing `stake_progression_source_order_id`.

- [ ] **Step 3: Extend the order model and simulator without breaking existing callers**

Add to `SimulatedOrder`:

```python
stake_progression_step: int = 1
stake_progression_source_order_id: Optional[int] = None
stake_progression_version: str = ""
```

Change `AccountSimulator.__init__` to accept `stake_progression_max_active`, `max_open_orders`, `stake_progression_activated_at`, and restored credits. Keep `open_order()` returning `SimulatedOrder`, but add `open_order_with_credit()` returning `(order, consumed_credit)` for `MonitorState`. Add `settle_expired_order_events()` and `settle_expired_order_events_from_klines()` returning immutable events containing `(order, created_credit)`; keep the old settlement methods as wrappers returning only orders so existing tests and analysis helpers remain compatible.

Use the ledger terms when constructing an order:

```python
terms, consumed_credit = self.stake_progression.assign(self._next_id, opened_at)
order = SimulatedOrder(
    id=self._next_id,
    direction=signal.direction,
    timeframe_minutes=signal.timeframe_minutes,
    level=signal.level,
    reason=signal.reason,
    entry_price=entry_price,
    opened_at=opened_at,
    expires_at=opened_at + signal.timeframe_minutes * 60_000,
    threshold_segment=signal.threshold_segment,
    score=signal.score,
    threshold=signal.threshold,
    session_allowed=signal.session_allowed,
    session_sample_size=signal.session_sample_size,
    session_win_rate=signal.session_win_rate,
    session_ev=signal.session_ev,
    session_edge_min=signal.session_edge_min,
    regime=signal.regime,
    strategy_family=signal.strategy_family,
    strategy_tag=signal.strategy_tag,
    profile_key=signal.profile_key,
    daily_profile_selected=signal.daily_profile_selected,
    daily_profile_version=signal.daily_profile_version,
    stake=terms.stake,
    win_return=terms.win_return,
    stake_progression_step=terms.step,
    stake_progression_source_order_id=terms.source_order_id,
    stake_progression_version=TWO_STAGE_VERSION if self.enable_stake_progression else "",
)
```

After `_settle_order`, call `ledger.settle()` with the settled order ID, opened time, stage, result, and settlement time. Remove the history-scanning `_next_order_terms()` implementation and segment exclusion branches. Expose the ledger `status()` fields through `stats()`.

- [ ] **Step 4: Run focused model and simulator tests**

Run:

```bash
python3 -m unittest tests.test_simulator tests.test_storage.SQLiteMonitorStoreTest.test_persists_and_restores_simulated_orders
```

Expected: all focused tests pass and old JSON order payloads still restore because new dataclass fields have defaults.

- [ ] **Step 5: Commit simulator integration**

```bash
git add app/models.py app/simulator.py tests/test_simulator.py
git commit -m "feat: apply strict two-stage stakes to simulated orders"
```

### Task 3: Persist progression runs and credits atomically

**Files:**
- Modify: `app/storage.py:345-389, 746-890`
- Modify: `tests/test_storage.py`

- [ ] **Step 1: Write failing storage tests**

Add tests that prove activation boundaries, credit recovery, idempotence, and atomic consumption:

```python
def test_prepares_persistent_two_stage_run_and_reuses_activation_on_restart(self):
    with tempfile.TemporaryDirectory() as temp_dir:
        store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
        first = store.prepare_stake_progression("BTCUSDT", "TWO_STAGE_V1", True, 1_000)
        second = store.prepare_stake_progression("BTCUSDT", "TWO_STAGE_V1", True, 2_000)
    self.assertEqual((first, second), (1_000, 1_000))

def test_settlement_and_credit_are_saved_in_one_transaction(self):
    with tempfile.TemporaryDirectory() as temp_dir:
        store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
        order = SimulatedOrder(1, "LONG", 10, "A", "win", 100.0, 1_000, 601_000,
                               status="SETTLED", result="WIN", settled_at=601_000, pnl=8.0)
        credit = StakeProgressionCredit(source_order_id=1, created_at=601_000)
        store.save_settled_order_with_credit(order, "BTCUSDT", credit)
        restored_orders = store.load_orders("BTCUSDT")
        restored_credits = store.load_stake_progression_credits("BTCUSDT", "TWO_STAGE_V1")
    self.assertEqual(restored_orders[0].result, "WIN")
    self.assertEqual(restored_credits[0].status, "PENDING")

def test_open_order_consumes_credit_exactly_once(self):
    with tempfile.TemporaryDirectory() as temp_dir:
        store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
        credit = StakeProgressionCredit(source_order_id=1, created_at=601_000)
        source = SimulatedOrder(1, "LONG", 10, "A", "source", 100.0, 1_000, 601_000,
                                status="SETTLED", result="WIN", settled_at=601_000, pnl=8.0)
        store.save_settled_order_with_credit(source, "BTCUSDT", credit)
        second = SimulatedOrder(2, "SHORT", 10, "A", "consume", 101.0, 602_000, 1_202_000,
                                stake=18.0, win_return=32.4, stake_progression_step=2,
                                stake_progression_source_order_id=1,
                                stake_progression_version="TWO_STAGE_V1")
        credit.status = "CONSUMED"
        credit.consumed_order_id = 2
        credit.consumed_at = 602_000
        store.save_open_order_with_credit(second, "BTCUSDT", credit)
        restored = store.load_stake_progression_credits("BTCUSDT", "TWO_STAGE_V1")
    self.assertEqual((restored[0].status, restored[0].consumed_order_id), ("CONSUMED", 2))
```

- [ ] **Step 2: Run storage tests and verify missing methods**

Run `python3 -m unittest tests.test_storage`.

Expected: `AttributeError` for `prepare_stake_progression` and the credit transaction methods.

- [ ] **Step 3: Add schema and transactional APIs**

Add these tables in `_init_schema()`:

```sql
create table if not exists stake_progression_runtime (
    symbol text primary key,
    version text not null,
    activated_at integer not null,
    enabled integer not null,
    updated_at_ms integer not null default (strftime('%s','now') * 1000)
);

create table if not exists stake_progression_credits (
    symbol text not null,
    version text not null,
    source_order_id integer not null,
    status text not null,
    created_at integer not null,
    consumed_order_id integer,
    consumed_at integer,
    updated_at_ms integer not null default (strftime('%s','now') * 1000),
    primary key(symbol, version, source_order_id),
    unique(symbol, version, consumed_order_id)
);
```

Extract the existing order UPSERT into `_upsert_order(connection, order, symbol)`. Implement:

```python
def save_settled_order_with_credit(self, order, symbol, credit):
    with self._connect() as connection:
        self._upsert_order(connection, order, symbol)
        if credit is not None:
            self._upsert_progression_credit(connection, symbol, credit)

def save_open_order_with_credit(self, order, symbol, credit):
    with self._connect() as connection:
        self._upsert_order(connection, order, symbol)
        if credit is not None:
            self._upsert_progression_credit(connection, symbol, credit)

def save_stake_progression_credit(self, symbol, credit):
    with self._connect() as connection:
        self._upsert_progression_credit(connection, symbol, credit)
```

`prepare_stake_progression()` must retain `activated_at` across restarts of the same enabled version. A version change or disabled-to-enabled transition starts a new activation time and cancels every `PENDING` credit for the symbol. Disabling also cancels pending credits. `load_stake_progression_credits()` returns typed `StakeProgressionCredit` objects ordered by `created_at, source_order_id`.

- [ ] **Step 4: Run storage and migration tests**

Run `python3 -m unittest tests.test_storage`.

Expected: all storage tests pass against fresh temporary databases, including loading legacy order JSON without the new source fields.

- [ ] **Step 5: Commit persistence**

```bash
git add app/storage.py tests/test_storage.py
git commit -m "feat: persist two-stage progression credits"
```

### Task 4: Wire settlement-first progression into monitor state

**Files:**
- Modify: `app/state.py:25-130, 132-182, 207-235, 322-328, 731-745, 864-894`
- Modify: `tests/test_state.py`
- Modify: `tests/test_server.py`

- [ ] **Step 1: Write failing state and API tests**

Add a state test with temporary SQLite storage that opens a 10U order, settles it as a win, consumes the resulting credit on a previously excluded segment, and checks the API status:

```python
def test_state_persists_and_exposes_two_stage_progression(self):
    def make_signal(direction, segment, opened_at):
        return Signal(
            direction=direction,
            timeframe_minutes=10,
            level="A",
            reason="two-stage integration",
            price=100.0,
            open_time=opened_at,
            score=90.0 if direction == "LONG" else -90.0,
            threshold=70.0,
            threshold_segment=segment,
            session_allowed=True,
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
        state = MonitorState(
            symbol="BTCUSDT",
            storage=store,
            enable_stake_progression=True,
            stake_progression_max_active=1,
            max_open_orders=5,
            now_ms=lambda: 1_000,
        )
        first = state.simulator.open_order(make_signal("LONG", "WD-08", 0), 100.0, 1_000)
        events = state.simulator.settle_expired_order_events(601_000, 101.0)
        state._persist_settlement_events(events)
        second, consumed = state.simulator.open_order_with_credit(
            make_signal("SHORT", "WD-23", 601_000), 101.0, 602_000
        )
        state._persist_open_order(second, consumed)
        state.wait_for_storage_writes()

        restarted = MonitorState(
            symbol="BTCUSDT",
            storage=store,
            enable_stake_progression=True,
            stake_progression_max_active=1,
            max_open_orders=5,
            now_ms=lambda: 2_000,
        )

    self.assertEqual((first.stake, second.stake), (10.0, 18.0))
    self.assertEqual(restarted.snapshot()["stake_progression"]["active_second_orders"], 1)
    self.assertEqual(restarted.snapshot()["stake_progression"]["pending_credits"], 0)
```

Extend `tests/test_server.py` so `/api/state` asserts:

```python
self.assertEqual(state_payload["stake_progression"]["max_orders"], 2)
self.assertEqual(state_payload["stake_progression"]["max_active"], 1)
self.assertIn("next_stake", state_payload["stake_progression"])
```

- [ ] **Step 2: Run focused state tests and verify failures**

Run:

```bash
python3 -m unittest tests.test_state tests.test_server
```

Expected: constructor argument, persistence helper, and API field failures.

- [ ] **Step 3: Initialize and restore progression state**

Add `stake_progression_max_active: int = 1` and an injectable `now_ms` callable to `MonitorState`. Before constructing `AccountSimulator`:

1. Call `storage.prepare_stake_progression(symbol, TWO_STAGE_VERSION, enabled, now_ms())` when storage exists.
2. Load current-version credits.
3. Count restored open orders where `stake_progression_step == 2` and version matches.
4. Pass activation, credits, active count, total open-order limit, and effective max-active value to `AccountSimulator`.

Repeat this initialization in `reset_symbol()` through one private `_build_simulator()` helper instead of duplicating it.

When ledger restoration changes any loaded `PENDING` credit to `CANCELLED` because progression is disabled or capacity was reduced, enqueue those changed credits through `save_stake_progression_credit()`. `MonitorState` stores a `stake_progression_recovery_warning` string and merges it into the top-level `stake_progression` snapshot as `recovery_warning`. Extend `RecordingStorage` in `tests/test_state.py` with `prepare_stake_progression`, `load_stake_progression_credits`, `save_stake_progression_credit`, `save_settled_order_with_credit`, and `save_open_order_with_credit` so state unit tests exercise the same interface as SQLite.

- [ ] **Step 4: Persist order/credit pairs in monitor event order**

At `update_from_klines()` use settlement events rather than plain settled orders. Submit one executor task per event that calls `save_settled_order_with_credit()`, then update the entry snapshot settlement. At opening use `open_order_with_credit()` and submit `save_open_order_with_credit()` before queuing the entry snapshot and webhook.

Add this top-level snapshot field:

```python
"stake_progression": self.simulator.stake_progression.status(),
```

Add the same status plus `source_order_id` and version to the order entry snapshot. Remove the phrase `固定基础金额` from promoted SHORT reasons because every selected segment can now consume a second-stage credit.

- [ ] **Step 5: Run state, server, storage, and webhook tests**

Run:

```bash
python3 -m unittest tests.test_state tests.test_server tests.test_storage tests.test_webhook
```

Expected: all tests pass; webhook tests show `amount=18.0` only for a consumed second-stage credit.

- [ ] **Step 6: Commit monitor integration**

```bash
git add app/state.py tests/test_state.py tests/test_server.py
git commit -m "feat: coordinate persistent two-stage progression"
```

### Task 5: Add startup configuration and dashboard status

**Files:**
- Modify: `app/server.py:255-312, 450-470`
- Modify: `scripts/run.sh:20-27, 74-86, argument parser, final exec`
- Modify: `tests/test_packaging.py`
- Modify: `app/static/index.html:13-20`
- Modify: `app/static/app.js`
- Modify: `README.md`
- Modify: `docs/current-strategy.md:1042-1075`

- [ ] **Step 1: Write failing launch-script tests**

Extend the existing environment forwarding case in `tests/test_packaging.py`:

```python
env.update({
    "STAKE_PROGRESSION": "1",
    "STAKE_PROGRESSION_MAX_ORDERS": "2",
    "STAKE_PROGRESSION_MAX_ACTIVE": "3",
    "STAKE_PROGRESSION_BASE_ONLY_SEGMENTS": "",
})
self.assertEqual(args[args.index("--stake-progression-max-orders") + 1], "2")
self.assertEqual(args[args.index("--stake-progression-max-active") + 1], "3")
self.assertEqual(args[args.index("--stake-progression-base-only-segments") + 1], "")
```

Also assert `scripts/run.sh --help` contains Chinese descriptions and defaults for two-stage progression and maximum simultaneous second-stage orders.

- [ ] **Step 2: Run packaging tests and verify the new argument is absent**

Run `python3 -m unittest tests.test_packaging`.

Expected: failure because `--stake-progression-max-active` is not forwarded.

- [ ] **Step 3: Implement CLI and environment defaults**

Set these defaults in `app/server.py` and `scripts/run.sh`:

```text
STAKE_PROGRESSION=1
STAKE_PROGRESSION_MAX_ORDERS=2
STAKE_PROGRESSION_MAX_ACTIVE=1
STAKE_PROGRESSION_BASE_ONLY_SEGMENTS=
```

Add `--stake-progression-max-active N` with Chinese help text. Forward it into `MonitorState`. Retain `--stake-progression-max-orders` and the base-only option for compatibility, but document that production two-stage mode uses `2` and an empty list.

- [ ] **Step 4: Render live progression state**

Change the static badge to:

```html
<span id="stake-progression-badge">两单叠加</span>
```

In the state renderer set:

```javascript
const progression = state.stake_progression || {};
$("stake-progression-badge").textContent = progression.enabled
  ? `两单叠加 · 18U订单 ${progression.active_second_orders || 0}/${progression.max_active || 1} · 待用资格 ${progression.pending_credits || 0}`
  : "两单叠加 OFF";
```

Use `next_stake` from the API instead of hard-coding 18U when the base amount is configurable. Update README and current strategy documentation to describe all-segment eligibility, the `1..MAX_OPEN_ORDERS` bound, restart recovery, and the maximum default exposure of 58U.

- [ ] **Step 5: Run packaging and JavaScript checks**

Run:

```bash
python3 -m unittest tests.test_packaging tests.test_server
node --check app/static/app.js
bash scripts/run.sh --help
```

Expected: tests and syntax checks pass; help output is Chinese and shows defaults `2` and `1`.

- [ ] **Step 6: Commit configuration and dashboard changes**

```bash
git add app/server.py scripts/run.sh tests/test_packaging.py app/static/index.html app/static/app.js README.md docs/current-strategy.md
git commit -m "feat: configure and display two-stage progression"
```

### Task 6: Reprice strict daily-profile replay with two-stage stakes

**Files:**
- Modify: `app/backtest.py:10-27, 60-150, 210-227`
- Modify: `tests/test_backtest.py:130-199`
- Create: `scripts/replay_two_stage_stakes.py`
- Create: `tests/test_two_stage_stake_replay.py`

- [ ] **Step 1: Write failing replay tests for concurrent credits**

Create `tests/test_two_stage_stake_replay.py` using synthetic trade rows where two base orders overlap, the first wins, and only the next eligible order receives 18U:

```python
import unittest

from scripts.replay_two_stage_stakes import reprice_trades


class TwoStageStakeReplayTest(unittest.TestCase):
    def test_reprice_preserves_orders_and_results_but_limits_second_stage(self):
        rows = [
            {"opened_at": 0, "expires_at": 600_000, "direction": "LONG", "result": "WIN", "profile_key": "LONG|WD-08"},
            {"opened_at": 120_000, "expires_at": 720_000, "direction": "SHORT", "result": "WIN", "profile_key": "SHORT|WD-23"},
            {"opened_at": 600_000, "expires_at": 1_200_000, "direction": "LONG", "result": "LOSS", "profile_key": "LONG|WD-12"},
            {"opened_at": 840_000, "expires_at": 1_440_000, "direction": "SHORT", "result": "WIN", "profile_key": "SHORT|WD-18"},
        ]

        result = reprice_trades(rows, base_stake=10.0, base_win_return=18.0, max_active=1)

        self.assertEqual([item["stake"] for item in result["trade_rows"]], [10.0, 10.0, 18.0, 10.0])
        self.assertEqual([item["result"] for item in result["trade_rows"]], ["WIN", "WIN", "LOSS", "WIN"])
        self.assertEqual(result["summary"]["orders"], 4)
        self.assertEqual(result["summary"]["second_stage_orders"], 1)
        self.assertEqual(result["summary"]["pnl"], 6.0)
```

- [ ] **Step 2: Run the replay test and verify the missing module failure**

Run `python3 -m unittest tests.test_two_stage_stake_replay`.

Expected: `ModuleNotFoundError` for `scripts.replay_two_stage_stakes`.

- [ ] **Step 3: Reuse the ledger in generic backtest and repricing**

Add `stake_progression_max_active: int = 1` to `BacktestConfig`. Replace `next_stake` and `stake_progression_step` globals with `TwoStageStakeProgression`; process all expiries before entries at the same timestamp. Update old tests to expect `10, 18, 10` rather than step 3.

Create `scripts/replay_two_stage_stakes.py` with:

```python
from collections import defaultdict
from copy import deepcopy

from app.stake_progression import TwoStageStakeProgression


def reprice_trades(rows, *, base_stake=10.0, base_win_return=18.0, max_active=1):
    trades = [deepcopy(item) for item in rows]
    if not trades:
        return {
            "config": {"base_stake": base_stake, "base_win_return": base_win_return, "max_active": max_active},
            "summary": _summary([]),
            "by_direction": [],
            "by_profile": [],
            "trade_rows": [],
        }

    activated_at = min(int(item["opened_at"]) for item in trades)
    ledger = TwoStageStakeProgression(
        base_stake=base_stake,
        base_win_return=base_win_return,
        enabled=True,
        max_active=max_active,
        max_open_orders=5,
        activated_at=activated_at,
    )
    events = []
    indexed = {}
    for index, item in enumerate(trades):
        order_id = int(item.get("id") or index + 1)
        item["id"] = order_id
        item["settled_at"] = int(item.get("settled_at") or item["expires_at"])
        indexed[order_id] = item
        events.append((int(item["opened_at"]), 1, order_id))
        events.append((item["settled_at"], 0, order_id))

    open_stakes = {}
    balance = 0.0
    peak_balance = 0.0
    max_drawdown = 0.0
    peak_open_stake = 0.0
    for event_time, event_kind, order_id in sorted(events):
        item = indexed[order_id]
        if event_kind == 1:
            terms, _credit = ledger.assign(order_id, event_time)
            item["stake"] = terms.stake
            item["win_return"] = terms.win_return
            item["stake_progression_step"] = terms.step
            item["stake_progression_source_order_id"] = terms.source_order_id
            open_stakes[order_id] = terms.stake
            peak_open_stake = max(peak_open_stake, sum(open_stakes.values()))
            continue

        stake = float(item["stake"])
        win_return = float(item["win_return"])
        item["pnl"] = round(win_return - stake, 4) if item["result"] == "WIN" else round(-stake, 4)
        open_stakes.pop(order_id, None)
        balance = round(balance + item["pnl"], 4)
        peak_balance = max(peak_balance, balance)
        max_drawdown = max(max_drawdown, peak_balance - balance)
        ledger.settle(
            order_id,
            int(item["opened_at"]),
            int(item["stake_progression_step"]),
            item["result"],
            event_time,
        )

    ordered = sorted(trades, key=lambda item: (item["opened_at"], item["id"]))
    summary = _summary(ordered)
    summary["max_drawdown"] = round(max_drawdown, 4)
    summary["peak_open_stake"] = round(peak_open_stake, 4)
    return {
        "config": {"base_stake": base_stake, "base_win_return": base_win_return, "max_active": ledger.max_active},
        "summary": summary,
        "by_direction": _group(ordered, "direction"),
        "by_profile": _group(ordered, "profile_key"),
        "trade_rows": ordered,
    }


def _summary(rows):
    wins = sum(item.get("result") == "WIN" for item in rows)
    pnl = round(sum(float(item.get("pnl", 0.0)) for item in rows), 4)
    total_staked = round(sum(float(item.get("stake", 0.0)) for item in rows), 4)
    second = [item for item in rows if int(item.get("stake_progression_step", 1)) == 2]
    second_wins = sum(item.get("result") == "WIN" for item in second)
    return {
        "orders": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "win_rate": round(wins / len(rows), 6) if rows else 0.0,
        "pnl": pnl,
        "ev": round(pnl / len(rows), 4) if rows else 0.0,
        "total_staked": total_staked,
        "roi": round(pnl / total_staked, 6) if total_staked else 0.0,
        "second_stage_orders": len(second),
        "second_stage_wins": second_wins,
        "second_stage_win_rate": round(second_wins / len(second), 6) if second else 0.0,
    }


def _group(rows, key):
    groups = defaultdict(list)
    for item in rows:
        groups[str(item.get(key) or "UNKNOWN")].append(item)
    return [{key: value, **_summary(items)} for value, items in sorted(groups.items())]
```

The CLI accepts `--input` daily-profile replay JSON and `--output`. It calls `reprice_trades()` for two-stage `max_active=1..5`. It also emits a fixed 10U baseline by assigning every trade `stake=10`, `win_return=18`, and step 1, plus a legacy three-stage baseline that retains the previous settlement-ordered `next_stake` transition solely for comparison. Assert before writing the report that every policy has the same ordered `(id, direction, result)` sequence.

- [ ] **Step 4: Run backtest and repricing tests**

Run:

```bash
python3 -m unittest tests.test_backtest tests.test_two_stage_stake_replay tests.test_daily_profile_replay
```

Expected: all tests pass; fixed, legacy, and new policy results retain identical order IDs, directions, and WIN/LOSS sequences.

- [ ] **Step 5: Commit replay support**

```bash
git add app/backtest.py tests/test_backtest.py scripts/replay_two_stage_stakes.py tests/test_two_stage_stake_replay.py
git commit -m "feat: replay two-stage progression exposure"
```

### Task 7: Verify, replay server data, and deploy

**Files:**
- Verify: `README.md`, `docs/current-strategy.md`
- Generate outside git: `backtests/two-stage-stake-*.json`
- Server config: `/etc/systemd/system/victory-event-monitor.service.d/40-two-stage-stake.conf`

- [ ] **Step 1: Run the full local verification suite**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
git diff --check
node --check app/static/app.js
bash scripts/run.sh --help
```

Expected: every test passes, no whitespace errors, JavaScript parses, and help text reports two-stage defaults.

- [ ] **Step 2: Download a compact current server sample without stopping production**

Use an ephemeral SSH key file outside the repository. On the server, create `/tmp/monitor-two-stage-replay.sqlite3` containing only `orders`, `observation_signals`, and `daily_profile_selections`, then copy it to `/private/tmp`. Do not copy the full audit-heavy database and do not clear production orders.

- [ ] **Step 3: Run strict daily selection and stake sensitivity replay**

Run:

```bash
python3 scripts/replay_daily_profile_selector.py \
  --db-path /private/tmp/monitor-two-stage-replay.sqlite3 \
  --symbol BTCUSDT \
  --min-samples 20 \
  --min-win-rate 0.60 \
  --min-ev 0 \
  --max-open-orders 5 \
  --min-order-gap-minutes 2 \
  --output backtests/daily-profile-two-stage-source.json

python3 scripts/replay_two_stage_stakes.py \
  --input backtests/daily-profile-two-stage-source.json \
  --output backtests/two-stage-stake-sensitivity.json
```

Expected: all policies have identical order count and win rate. Report PnL, EV, ROI, maximum drawdown, peak stake exposure, and second-stage results for limits 1 through 5. Default limit 1 is deployable only if it does not worsen maximum drawdown beyond fixed 10U without a documented expected-return improvement.

- [ ] **Step 4: Package the verified release**

Run `bash scripts/package.sh --output-dir /private/tmp/victory-two-stage-release --name event-contract-monitor-<timestamp>` and record the archive SHA-256. Verify imports from the extracted archive before upload.

- [ ] **Step 5: Install explicit production configuration and switch release**

Install this independent systemd drop-in:

```ini
[Service]
Environment=STAKE_PROGRESSION=1
Environment=STAKE_PROGRESSION_MAX_ORDERS=2
Environment=STAKE_PROGRESSION_MAX_ACTIVE=1
Environment="STAKE_PROGRESSION_BASE_ONLY_SEGMENTS="
```

Create a compact pre-release backup of orders, observation signals, daily selections, progression runtime, and progression credits. Extract the release, run import/schema checks, atomically update `/opt/victory-event-monitor/current`, and restart `victory-event-monitor`. Existing open orders remain untouched and cannot create a credit if opened before the persisted activation boundary.

- [ ] **Step 6: Verify production behavior**

Check:

```text
systemctl is-active victory-event-monitor = active
/api/state.stake_progression.enabled = true
/api/state.stake_progression.max_orders = 2
/api/state.stake_progression.max_active = 1
/api/state.warmup.status = READY
/api/state.last_error = null
HTTPS status = 200, certificate result = 0
journal warning entries since deployment = none
```

Confirm the page says `两单叠加`, historical orders remain present, and the first post-release 10U win creates at most one pending qualification. Do not wait for or force a real 18U order; the API and persisted credit state are sufficient deployment checks.

- [ ] **Step 7: Commit any final report documentation only if files changed**

If Task 7 required tracked documentation corrections, stage only those files and commit:

```bash
git add README.md docs/current-strategy.md
git commit -m "docs: record two-stage progression rollout"
```

Do not stage generated databases, replay JSON, private keys, release archives, or unrelated dirty-worktree files.
