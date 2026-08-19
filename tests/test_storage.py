import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing, contextmanager
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from unittest import mock
from pathlib import Path

from app.decision_context import (
    CONTEXT_VERSION,
    DecisionContext,
    RuntimeConfigSnapshot,
    runtime_config_snapshot,
)
from app.models import ObservationSignal, Signal, SimulatedOrder
from app import order_profile
from app.simulator import AccountSimulator
from app.stake_progression import TWO_STAGE_VERSION, StakeProgressionCredit
from app.storage import DecisionAudit, SQLiteMonitorStore
from app.storage_capacity import (
    COMPACT_ONLY_BYTES,
    CoreStorageCapacityError,
    capacity_for_bytes,
)
from app.wave_state import WaveSnapshot


def signal(direction="LONG", timeframe_minutes=10):
    return Signal(
        direction=direction,
        timeframe_minutes=timeframe_minutes,
        level="A",
        reason="persist me",
        price=100.0,
        open_time=0,
        score=82.0,
        threshold=70.0,
        calculated_threshold=81.5,
        threshold_segment="WD-12",
        session_allowed=True,
        session_sample_size=37,
        session_win_rate=0.6757,
        session_ev=2.1622,
        session_edge_min=10.0,
        regime="FEAR_RISING",
    )


def observation(
    key: str,
    *,
    family: str = "short_extension",
    tag: str = "normal_down_short_extension_observe",
    direction: str = "SHORT",
    segment: str = "WD-02",
    status: str = "SETTLED",
    result: str | None = "WIN",
    opened_at: int = 1_000,
) -> ObservationSignal:
    pnl = 0.0
    if status == "SETTLED":
        pnl = 8.0 if result == "WIN" else -10.0
    return ObservationSignal(
        observation_key=key,
        strategy_family=family,
        strategy_tag=tag,
        direction=direction,
        timeframe_minutes=10,
        level="A",
        reason="observe signal",
        entry_price=100.0,
        opened_at=opened_at,
        expires_at=opened_at + 600_000,
        threshold_segment=segment,
        score=-90.0 if direction == "SHORT" else 90.0,
        threshold=80.0,
        edge=10.0,
        regime="FEAR_FALLING",
        source_decision="SHORT_OBSERVE_ONLY",
        status=status,
        result=result,
        exit_price=99.0 if result == "WIN" else 101.0,
        settled_at=opened_at + 600_000 if status == "SETTLED" else None,
        pnl=pnl,
    )


def progression_order(
    order_id: int,
    *,
    status: str = "OPEN",
    step: int = 1,
    source_order_id: int | None = None,
    version: str = TWO_STAGE_VERSION,
) -> SimulatedOrder:
    settled = status == "SETTLED"
    return SimulatedOrder(
        id=order_id,
        direction="LONG",
        timeframe_minutes=10,
        level="A",
        reason="progression persistence",
        entry_price=100.0,
        opened_at=1_000,
        expires_at=601_000,
        status=status,
        result="WIN" if settled else None,
        exit_price=101.0 if settled else None,
        settled_at=601_000 if settled else None,
        pnl=8.0 if settled else 0.0,
        stake_progression_step=step,
        stake_progression_source_order_id=source_order_id,
        stake_progression_version=version,
    )


def decision_context(
    snapshot: RuntimeConfigSnapshot,
    **overrides,
) -> DecisionContext:
    arguments = {
        "decision_id": "decision-1",
        "context_version": CONTEXT_VERSION,
        "runtime_config_hash": snapshot.hash,
        "strategy_build_id": snapshot.strategy_build_id,
        "symbol": "BTCUSDT",
        "closed_kline_at_ms": 1_700_000_000_000,
        "candidate_origin": "strategy",
        "inputs": {
            "identity": {
                "direction": "LONG",
                "profile_key": "profile-a",
            },
            "market": {"close": 100.25},
        },
        "decision_trace": (
            {
                "stage": "qualification",
                "result": "PASS",
                "decisive_values": {"score": 82.0},
                "reason_code": "QUALIFIED",
            },
        ),
        "first_decisive_block": "",
        "final_decision": "OPEN",
        "final_reason": "accepted",
        "open_allowed": True,
        "observation_allowed": False,
    }
    arguments.update(overrides)
    return DecisionContext(**arguments)


def snapshot_with_payload(
    canonical_payload: str,
    *,
    strategy_build_id: str = "build-7",
    runtime_config_hash: str | None = None,
) -> RuntimeConfigSnapshot:
    return RuntimeConfigSnapshot(
        hash=runtime_config_hash
        or hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest(),
        canonical_payload=canonical_payload,
        strategy_build_id=strategy_build_id,
    )


def insert_runtime_config_row(
    db_path: Path,
    snapshot: RuntimeConfigSnapshot,
    **overrides,
) -> None:
    values = {
        "runtime_config_hash": snapshot.hash,
        "context_version": CONTEXT_VERSION,
        "strategy_build_id": snapshot.strategy_build_id,
        "canonical_payload": snapshot.canonical_payload,
        "payload_bytes": len(snapshot.canonical_payload.encode("utf-8")),
        "created_at_ms": 123,
    }
    values.update(overrides)
    columns = tuple(values)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            f"""
            insert into runtime_config_snapshots({", ".join(columns)})
            values ({", ".join("?" for _ in columns)})
            """,
            tuple(values[column] for column in columns),
        )
        connection.commit()


def decision_storage_rows(db_path: Path):
    with closing(sqlite3.connect(db_path)) as connection:
        runtime_rows = connection.execute(
            "select * from runtime_config_snapshots order by runtime_config_hash"
        ).fetchall()
        decision_rows = connection.execute(
            "select * from decision_contexts order by symbol, decision_id"
        ).fetchall()
    return runtime_rows, decision_rows


def atomic_bundle_fixture(*, include_observation: bool = True):
    config = runtime_config_snapshot(
        {"stake": 10.0, "max_open_orders": 2, "guards": {"wave": False}},
        strategy_build_id="build-task-7",
    )
    context = decision_context(
        config,
        decision_id="decision-task-7",
        closed_kline_at_ms=601_000,
        candidate_origin="NATIVE_ACTIONABLE",
        inputs={
            "identity": {
                "direction": "LONG",
                "profile_key": "10|drop_reclaim|live|LONG|WD-12",
            },
            "market": {"close": 100.0},
        },
        final_decision="OPENED",
        final_reason="accepted",
        open_allowed=True,
        observation_allowed=include_observation,
    )
    decision_metadata = {
        "decision_id": context.decision_id,
        "context_version": context.context_version,
        "runtime_config_hash": context.runtime_config_hash,
        "strategy_build_id": context.strategy_build_id,
        "candidate_origin": context.candidate_origin,
    }
    audit_signal = replace(
        signal(),
        profile_key="10|drop_reclaim|live|LONG|WD-12",
        decision_inputs=context.to_dict()["inputs"],
        decision_trace=context.to_dict()["decision_trace"],
        first_decisive_block=context.first_decisive_block,
        **decision_metadata,
    )
    order = replace(
        progression_order(1, version=""),
        profile_key=audit_signal.profile_key,
        stake_progression_version="",
        decision_inputs=context.to_dict()["inputs"],
        decision_trace=context.to_dict()["decision_trace"],
        first_decisive_block=context.first_decisive_block,
        **decision_metadata,
    )
    observed = None
    if include_observation:
        observed = replace(
            observation(
                "decision-task-7-observation",
                family="drop_reclaim",
                tag="live",
                direction="LONG",
                segment="WD-12",
                status="OPEN",
                result=None,
                opened_at=601_000,
            ),
            profile_key=audit_signal.profile_key,
            source_decision="OPENED",
            decision_inputs=context.to_dict()["inputs"],
            decision_trace=context.to_dict()["decision_trace"],
            first_decisive_block=context.first_decisive_block,
            **decision_metadata,
        )
    audit = DecisionAudit(
        signal=audit_signal,
        decision="OPENED",
        created_at_ms=601_000,
        audit_context={"result_sequence_guard": {"status": "NORMAL"}},
        event_kind="ORDER_OPENED",
    )
    entry_snapshot = {
        "signal": audit_signal.to_dict(),
        "latest_kline": {"close_time": 601_000, "close": 100.0},
    }
    return config, context, order, audit, entry_snapshot, observed


def atomic_bundle_counts(db_path: Path) -> dict[str, int]:
    tables = (
        "runtime_config_snapshots",
        "decision_contexts",
        "orders",
        "stake_progression_credits",
        "order_entry_snapshots",
        "signal_audit",
        "observation_signals",
    )
    with closing(sqlite3.connect(db_path)) as connection:
        return {
            table: connection.execute(f"select count(*) from {table}").fetchone()[0]
            for table in tables
        }


ENTRY_STRUCTURE_FIXTURE = {
    "entry_structure_version": "ENTRY_STRUCTURE_SHADOW_V1",
    "entry_structure_mode": "SHADOW_ONLY",
    "entry_structure_state": "SUPPORT_REJECTED",
    "entry_structure_bias": "CONFIRMED",
    "entry_structure_reason_code": "SUPPORT_HELD",
    "candidate_origin": "NATIVE_ACTIONABLE",
    "active_level_source": "RECENT_SWING",
    "detail": {
        "levels": [99.0, 100.0],
        "retest": {"status": "CONFIRMED", "bars": [1, 2]},
    },
}


def structured_atomic_bundle(
    *,
    include_observation: bool = True,
    legacy_without_top_level: bool = False,
):
    config, context, order, audit, entry_snapshot, observed = atomic_bundle_fixture(
        include_observation=include_observation
    )
    structure = json.loads(json.dumps(ENTRY_STRUCTURE_FIXTURE))
    order = replace(
        order,
        opened_at=context.closed_kline_at_ms,
        expires_at=context.closed_kline_at_ms + 600_000,
    )
    signal_payload = audit.signal.to_dict()
    signal_payload["entry_structure_shadow"] = json.loads(json.dumps(structure))
    inputs = {
        **context.to_dict()["inputs"],
        "identity": {
            "direction": order.direction,
            "profile_key": order.profile_key,
            "strategy_family": order.strategy_family,
            "strategy_tag": order.strategy_tag,
            "order_slot": order.order_slot,
            "order_slot_scope": order.order_slot_scope,
            "timeframe_minutes": order.timeframe_minutes,
            "threshold_segment": order.threshold_segment,
            "level": order.level,
        },
        "market": {
            "close": order.entry_price,
            "candidate_time_ms": order.opened_at,
            "entry_price": order.entry_price,
        },
        "score": {
            "edge": abs(order.score) - order.threshold,
            "quality_score": order.quality_score,
            "quality_score_version": order.quality_score_version,
            "quality_score_mode": order.quality_score_mode,
            "quality_score_context": order.quality_score_context,
            "quality_score_components": order.quality_score_components,
            "quality_score_inputs": order.quality_score_inputs,
        },
        "signal": signal_payload,
    }
    if not legacy_without_top_level:
        inputs["entry_structure"] = json.loads(json.dumps(structure))
    context = replace(
        context,
        inputs=inputs,
        selected_order_terms={
            "stake": order.stake,
            "win_return": order.win_return,
            "progression_step": order.stake_progression_step,
            "progression_source_order_id": order.stake_progression_source_order_id,
            "progression_version": order.stake_progression_version,
            "expires_at": order.expires_at,
            "timeframe_minutes": order.timeframe_minutes,
            "order_slot": order.order_slot,
            "order_slot_scope": order.order_slot_scope,
            "direction": order.direction,
            "entry_price": order.entry_price,
        },
    )
    metadata = {
        "entry_structure_shadow": json.loads(json.dumps(structure)),
        "decision_inputs": context.to_dict()["inputs"],
    }
    audit = replace(audit, signal=replace(audit.signal, **metadata))
    order = replace(order, **metadata)
    if observed is not None:
        observed = replace(
            observed,
            opened_at=order.opened_at,
            expires_at=order.expires_at,
            **metadata,
        )
    entry_snapshot = {
        **entry_snapshot,
        "signal": audit.signal.to_dict(),
    }
    return config, context, order, audit, entry_snapshot, observed


class SQLiteMonitorStoreTest(unittest.TestCase):
    def test_top_level_structure_is_authoritative_without_signal_alias(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            config, context, order, audit, entry_snapshot, observed = (
                structured_atomic_bundle()
            )
            inputs = context.to_dict()["inputs"]
            inputs["signal"].pop("entry_structure_shadow")
            context = replace(context, inputs=inputs)
            audit = replace(
                audit,
                signal=replace(
                    audit.signal,
                    decision_inputs=context.to_dict()["inputs"],
                ),
            )
            order = replace(order, decision_inputs=context.to_dict()["inputs"])
            observed = replace(observed, decision_inputs=context.to_dict()["inputs"])

            store.save_open_order_decision(
                config=config,
                context=context,
                order=order,
                credit=None,
                entry_snapshot=entry_snapshot,
                audit=audit,
                observation=observed,
            )

            restored = SQLiteMonitorStore(db_path).load_orders(context.symbol)[0]
            self.assertEqual(restored.entry_structure_shadow, ENTRY_STRUCTURE_FIXTURE)
            restored_context = SQLiteMonitorStore(db_path).load_decision_context(
                context.symbol,
                context.decision_id,
            )
            self.assertEqual(
                restored_context["inputs"]["signal"]["entry_structure_shadow"],
                ENTRY_STRUCTURE_FIXTURE,
            )

    def test_load_decision_context_rejects_tampered_structure_alias(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            config, context, _order, _audit, _entry_snapshot, _observed = (
                structured_atomic_bundle()
            )
            store.save_runtime_config_snapshot(config)
            store.save_decision_context(context)

            with closing(sqlite3.connect(db_path)) as connection:
                payload = json.loads(
                    connection.execute(
                        "select input_payload from decision_contexts"
                    ).fetchone()[0]
                )
                payload["signal"]["entry_structure_shadow"]["detail"][
                    "levels"
                ][1] = 101.0
                connection.execute(
                    "update decision_contexts set input_payload = ?",
                    (
                        json.dumps(
                            payload,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ),
                    ),
                )
                connection.commit()

            with self.assertRaisesRegex(ValueError, "structure"):
                SQLiteMonitorStore(db_path).load_decision_context(
                    context.symbol,
                    context.decision_id,
                )

    def test_load_decision_context_normalizes_legacy_structure_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            config, context, _order, _audit, _entry_snapshot, _observed = (
                structured_atomic_bundle(legacy_without_top_level=True)
            )
            store.save_runtime_config_snapshot(config)
            store.save_decision_context(context)

            restored = SQLiteMonitorStore(db_path).load_decision_context(
                context.symbol,
                context.decision_id,
            )
            top_level = restored["inputs"]["entry_structure"]
            signal_alias = restored["inputs"]["signal"]["entry_structure_shadow"]
            self.assertEqual(top_level, ENTRY_STRUCTURE_FIXTURE)
            self.assertEqual(signal_alias, ENTRY_STRUCTURE_FIXTURE)
            self.assertIsNot(top_level, signal_alias)
            self.assertIsNot(top_level["detail"], signal_alias["detail"])

    def test_structure_snapshot_roundtrips_and_reopens_with_canonical_denormalization(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as temp_dir:
                db_path = Path(temp_dir) / "monitor.sqlite3"
                store = SQLiteMonitorStore(db_path)
                config, context, order, audit, entry_snapshot, observed = (
                    structured_atomic_bundle(legacy_without_top_level=legacy)
                )
                store.save_open_order_decision(
                    config=config,
                    context=context,
                    order=order,
                    credit=None,
                    entry_snapshot=entry_snapshot,
                    audit=audit,
                    observation=observed,
                )

                reopened = SQLiteMonitorStore(db_path)
                restored_order = reopened.load_orders(context.symbol)[0]
                restored_observation = reopened.load_observations(context.symbol)[0]
                restored_context = reopened.load_decision_context(
                    context.symbol,
                    context.decision_id,
                )
                restored_snapshot = reopened.load_order_entry_snapshots(
                    context.symbol
                )[0]["entry_payload"]

                self.assertEqual(
                    restored_order.entry_structure_shadow,
                    ENTRY_STRUCTURE_FIXTURE,
                )
                self.assertEqual(
                    restored_observation.entry_structure_shadow,
                    ENTRY_STRUCTURE_FIXTURE,
                )
                self.assertEqual(
                    restored_snapshot["signal"]["entry_structure_shadow"],
                    ENTRY_STRUCTURE_FIXTURE,
                )
                self.assertEqual(
                    restored_context["inputs"]["signal"]["entry_structure_shadow"],
                    ENTRY_STRUCTURE_FIXTURE,
                )
                self.assertEqual(
                    restored_context["inputs"]["entry_structure"],
                    ENTRY_STRUCTURE_FIXTURE,
                )
                with closing(sqlite3.connect(db_path)) as connection:
                    denormalized = connection.execute(
                        "select candidate_origin, entry_structure_state, "
                        "entry_structure_bias, active_level_source "
                        "from observation_signals"
                    ).fetchone()
                self.assertEqual(
                    denormalized,
                    (
                        context.candidate_origin,
                        ENTRY_STRUCTURE_FIXTURE["entry_structure_state"],
                        ENTRY_STRUCTURE_FIXTURE["entry_structure_bias"],
                        ENTRY_STRUCTURE_FIXTURE["active_level_source"],
                    ),
                )

    def test_decision_audit_deep_copies_signal_structure_snapshot(self):
        _config, _context, _order, audit, _entry_snapshot, _observed = (
            structured_atomic_bundle()
        )
        source_structure = json.loads(json.dumps(ENTRY_STRUCTURE_FIXTURE))
        source_signal = replace(
            audit.signal,
            entry_structure_shadow=source_structure,
        )

        snapshot = DecisionAudit(
            signal=source_signal,
            decision=audit.decision,
            created_at_ms=audit.created_at_ms,
            audit_context=audit.audit_context,
            event_kind=audit.event_kind,
        )
        source_structure["detail"]["levels"].append(102.0)

        self.assertIsNot(snapshot.signal, source_signal)
        self.assertIsNot(snapshot.signal.entry_structure_shadow, source_structure)
        self.assertIsNot(
            snapshot.signal.entry_structure_shadow["detail"],
            source_structure["detail"],
        )
        self.assertEqual(
            snapshot.signal.entry_structure_shadow,
            ENTRY_STRUCTURE_FIXTURE,
        )

    def test_decision_audit_roundtrips_complete_canonical_structure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            config, context, order, audit, entry_snapshot, observed = (
                structured_atomic_bundle()
            )
            store.save_open_order_decision(
                config=config,
                context=context,
                order=order,
                credit=None,
                entry_snapshot=entry_snapshot,
                audit=audit,
                observation=observed,
            )

            with closing(sqlite3.connect(db_path)) as connection:
                payload = json.loads(
                    connection.execute(
                        "select payload from signal_audit"
                    ).fetchone()[0]
                )

            self.assertEqual(payload["structure"], ENTRY_STRUCTURE_FIXTURE)
            self.assertEqual(
                payload["structure"]["detail"]["retest"]["bars"],
                [1, 2],
            )

    def test_structure_context_aliases_must_be_deeply_equal(self):
        cases = {
            "nested list differs": {
                **ENTRY_STRUCTURE_FIXTURE,
                "detail": {
                    **ENTRY_STRUCTURE_FIXTURE["detail"],
                    "levels": [99.0, 101.0],
                },
            },
            "explicit None is not empty": None,
            "explicit empty is not populated": {},
        }
        for label, nested_structure in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                db_path = Path(temp_dir) / "monitor.sqlite3"
                store = SQLiteMonitorStore(db_path)
                config, context, order, audit, entry_snapshot, observed = (
                    structured_atomic_bundle()
                )
                context = replace(
                    context,
                    inputs={
                        **context.inputs,
                        "signal": {
                            **context.inputs["signal"],
                            "entry_structure_shadow": nested_structure,
                        },
                    },
                )

                with self.assertRaisesRegex(ValueError, "structure"):
                    store.save_open_order_decision(
                        config=config,
                        context=context,
                        order=order,
                        credit=None,
                        entry_snapshot=entry_snapshot,
                        audit=audit,
                        observation=observed,
                    )
                self.assertEqual(
                    atomic_bundle_counts(db_path),
                    {key: 0 for key in atomic_bundle_counts(db_path)},
                )

    def test_open_bundle_rejects_each_mutated_structure_without_partial_rows(self):
        mutations = {
            "audit signal": lambda values: {
                **values,
                "audit": replace(
                    values["audit"],
                    signal=replace(
                        values["audit"].signal,
                        entry_structure_shadow={"entry_structure_state": "MUTATED"},
                    ),
                ),
            },
            "order": lambda values: {
                **values,
                "order": replace(
                    values["order"],
                    entry_structure_shadow={"entry_structure_state": "MUTATED"},
                ),
            },
            "observation": lambda values: {
                **values,
                "observation": replace(
                    values["observation"],
                    entry_structure_shadow={"entry_structure_state": "MUTATED"},
                ),
            },
            "entry snapshot": lambda values: {
                **values,
                "entry_snapshot": {
                    **values["entry_snapshot"],
                    "signal": {
                        **values["entry_snapshot"]["signal"],
                        "entry_structure_shadow": {
                            "entry_structure_state": "MUTATED"
                        },
                    },
                },
            },
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                db_path = Path(temp_dir) / "monitor.sqlite3"
                store = SQLiteMonitorStore(db_path)
                config, context, order, audit, entry_snapshot, observed = (
                    structured_atomic_bundle()
                )
                arguments = mutate(
                    {
                        "config": config,
                        "context": context,
                        "order": order,
                        "credit": None,
                        "entry_snapshot": entry_snapshot,
                        "audit": audit,
                        "observation": observed,
                    }
                )

                with self.assertRaisesRegex(ValueError, "structure"):
                    store.save_open_order_decision(**arguments)
                self.assertEqual(
                    atomic_bundle_counts(db_path),
                    {key: 0 for key in atomic_bundle_counts(db_path)},
                )

    def test_open_bundle_requires_signal_entry_snapshot_structure(self):
        for label, include_top_level in (
            ("all structure aliases missing", False),
            ("top-level alias cannot replace signal snapshot", True),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                db_path = Path(temp_dir) / "monitor.sqlite3"
                store = SQLiteMonitorStore(db_path)
                config, context, order, audit, entry_snapshot, observed = (
                    structured_atomic_bundle()
                )
                snapshot_signal = dict(entry_snapshot["signal"])
                snapshot_signal.pop("entry_structure_shadow")
                entry_snapshot = {
                    **entry_snapshot,
                    "signal": snapshot_signal,
                }
                if include_top_level:
                    entry_snapshot["entry_structure_shadow"] = json.loads(
                        json.dumps(ENTRY_STRUCTURE_FIXTURE)
                    )
                else:
                    entry_snapshot.pop("entry_structure_shadow", None)

                with self.assertRaisesRegex(
                    ValueError,
                    "entry_snapshot signal entry structure",
                ):
                    store.save_open_order_decision(
                        config=config,
                        context=context,
                        order=order,
                        credit=None,
                        entry_snapshot=entry_snapshot,
                        audit=audit,
                        observation=observed,
                    )
                self.assertEqual(
                    atomic_bundle_counts(db_path),
                    {key: 0 for key in atomic_bundle_counts(db_path)},
                )

    def test_blocked_bundle_structure_mismatch_rolls_back_every_member(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            config, context, _order, audit, _entry_snapshot, observed = (
                structured_atomic_bundle()
            )
            decision_trace = (
                {
                    "stage": "profile",
                    "result": "BLOCK",
                    "decisive_values": {"status": "PAUSED"},
                    "reason_code": "PROFILE_BLOCKED",
                },
            )
            context = replace(
                context,
                decision_trace=decision_trace,
                first_decisive_block="profile",
                final_decision="PROFILE_BLOCKED",
                final_reason="blocked",
                open_allowed=False,
                selected_order_terms={},
            )
            audit = replace(
                audit,
                signal=replace(
                    audit.signal,
                    decision_trace=list(decision_trace),
                    first_decisive_block="profile",
                    entry_structure_shadow={"entry_structure_state": "MUTATED"},
                ),
                decision="PROFILE_BLOCKED",
            )
            observed = replace(
                observed,
                decision_trace=list(decision_trace),
                first_decisive_block="profile",
                source_decision="PROFILE_BLOCKED",
            )

            with self.assertRaisesRegex(ValueError, "structure"):
                store.save_decision_bundle(
                    config=config,
                    context=context,
                    audit=audit,
                    observation=observed,
                )
            self.assertEqual(
                atomic_bundle_counts(db_path),
                {key: 0 for key in atomic_bundle_counts(db_path)},
            )

    def test_old_decision_payload_without_structure_remains_read_only_compatible(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            config, context, order, audit, entry_snapshot, observed = (
                atomic_bundle_fixture()
            )
            snapshot_signal = dict(entry_snapshot["signal"])
            snapshot_signal.pop("entry_structure_shadow")
            entry_snapshot = {
                **entry_snapshot,
                "signal": snapshot_signal,
            }
            entry_snapshot.pop("entry_structure_shadow", None)
            store.save_open_order_decision(
                config=config,
                context=context,
                order=order,
                credit=None,
                entry_snapshot=entry_snapshot,
                audit=audit,
                observation=observed,
            )

            reopened = SQLiteMonitorStore(db_path)
            self.assertEqual(
                reopened.load_orders(context.symbol)[0].entry_structure_shadow,
                {},
            )
            self.assertEqual(
                reopened.load_observations(context.symbol)[0].entry_structure_shadow,
                {},
            )
            persisted_context = reopened.load_decision_context(
                context.symbol,
                context.decision_id,
            )
            self.assertNotIn("entry_structure", persisted_context["inputs"])
            self.assertNotIn("signal", persisted_context["inputs"])

    def test_profile_summary_generated_at_marks_completed_calculation(self):
        original_summary = order_profile._summary
        delayed = False

        def delayed_summary(*args, **kwargs):
            nonlocal delayed
            if not delayed:
                delayed = True
                time.sleep(0.04)
            return original_summary(*args, **kwargs)

        started_at = datetime.now(timezone.utc)
        with mock.patch.object(order_profile, "_summary", side_effect=delayed_summary):
            summary = order_profile.summarize_order_samples_with_guard([])
        completed_at = datetime.now(timezone.utc)
        generated_at = datetime.fromisoformat(summary["generated_at"])

        self.assertGreaterEqual(generated_at, started_at + timedelta(seconds=0.03))
        self.assertLessEqual(generated_at, completed_at)
        self.assertGreaterEqual(summary["elapsed_seconds"], 0.03)

    def test_profile_revision_is_transactional_monotonic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            order = SimulatedOrder(
                id=1,
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="revision",
                entry_price=100.0,
                opened_at=1_000,
                expires_at=601_000,
            )
            entry = {"signal": signal().to_dict()}

            store.save_order_entry_snapshot(order, "BTCUSDT", entry)
            first = store.profile_summary_revision("BTCUSDT")
            store.save_order_entry_snapshot(order, "BTCUSDT", entry)
            duplicate = store.profile_summary_revision("BTCUSDT")
            settled = replace(
                order,
                status="SETTLED",
                result="WIN",
                settled_at=601_000,
                exit_price=101.0,
                pnl=8.0,
            )
            store.update_order_entry_snapshot_settlement(settled, "BTCUSDT")
            final = SQLiteMonitorStore(db_path).profile_summary_revision("BTCUSDT")

            self.assertEqual((first, duplicate, final), (1, 1, 2))

    def test_profile_summary_is_persistent_and_cross_store_stale_aware(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            first_store = SQLiteMonitorStore(db_path)
            second_store = SQLiteMonitorStore(db_path)
            base = SimulatedOrder(
                id=1,
                direction="LONG",
                timeframe_minutes=10,
                level="A",
                reason="cross store",
                entry_price=100.0,
                opened_at=1_000,
                expires_at=601_000,
            )
            first_store.save_order_entry_snapshot(
                base,
                "BTCUSDT",
                {"signal": signal().to_dict()},
            )
            first_store.prepare_order_profile_summary("BTCUSDT")
            first_store.wait_for_profile_summary_rebuilds(timeout=10)

            persisted = second_store.profile_summary_snapshot("BTCUSDT")
            self.assertEqual(persisted["cache_status"], "READY")
            self.assertEqual(persisted["snapshot_count"], 1)

            second = replace(base, id=2, opened_at=2_000, expires_at=602_000)
            with mock.patch.object(
                second_store,
                "_schedule_profile_summary_rebuild",
                return_value=None,
            ):
                second_store.save_order_entry_snapshot(
                    second,
                    "BTCUSDT",
                    {"signal": replace(signal(), open_time=2_000).to_dict()},
                )
            stale = first_store.profile_summary_snapshot("BTCUSDT")
            self.assertEqual(stale["cache_status"], "STALE")
            self.assertEqual(stale["source_revision"], 1)
            self.assertEqual(stale["current_revision"], 2)
            self.assertTrue(stale["stale"])

            first_store.wait_for_profile_summary_rebuilds(timeout=10)
            refreshed = second_store.profile_summary_snapshot("BTCUSDT")
            self.assertEqual(refreshed["cache_status"], "READY")
            self.assertEqual(refreshed["snapshot_count"], 2)

    def test_older_profile_revision_cannot_overwrite_new_materialization(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            key = store._profile_summary_key("BTCUSDT", 5000, 15, 2)
            with store._connect() as connection:
                store._bump_profile_summary_revision(connection, "BTCUSDT")
                store._bump_profile_summary_revision(connection, "BTCUSDT")

            newer = {"snapshot_count": 2, "marker": "new"}
            older = {"snapshot_count": 1, "marker": "old"}
            self.assertTrue(store._write_profile_summary_materialization(key, 2, newer))
            self.assertFalse(store._write_profile_summary_materialization(key, 1, older))

            summary = store.profile_summary_snapshot("BTCUSDT")
            self.assertEqual(summary["marker"], "new")
            self.assertEqual(summary["source_revision"], 2)

    def test_profile_worker_can_drain_and_close_without_leaking_thread(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.prepare_order_profile_summary("BTCUSDT")
            store.wait_for_profile_summary_rebuilds(timeout=10)
            store.close()

            worker_threads = [
                thread
                for thread in threading.enumerate()
                if thread.name.startswith(store.profile_worker_thread_prefix)
            ]
            self.assertEqual(worker_threads, [])
            with self.assertRaises(RuntimeError):
                store.prepare_order_profile_summary("BTCUSDT")

    def test_profile_rebuild_is_single_flight_under_concurrent_reads(self):
        class BlockingStore(SQLiteMonitorStore):
            def __init__(self, path):
                self.compute_started = threading.Event()
                self.compute_release = threading.Event()
                self.compute_calls = 0
                super().__init__(path)

            def _compute_profile_summary(self, key, samples):
                self.compute_calls += 1
                self.compute_started.set()
                self.compute_release.wait(timeout=5)
                return super()._compute_profile_summary(key, samples)

        with tempfile.TemporaryDirectory() as temp_dir:
            store = BlockingStore(Path(temp_dir) / "monitor.sqlite3")
            store.prepare_order_profile_summary("BTCUSDT")
            self.assertTrue(store.compute_started.wait(timeout=5))
            errors = []

            def read_summary():
                try:
                    for _ in range(10):
                        page = store.profile_summary_snapshot("BTCUSDT")
                        self.assertEqual(page["cache_status"], "PREPARING")
                except Exception as exc:  # noqa: BLE001 - captures thread failure.
                    errors.append(exc)

            threads = [threading.Thread(target=read_summary) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            store.compute_release.set()
            store.wait_for_profile_summary_rebuilds(timeout=10)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(store.compute_calls, 1)

    def assert_save_rejects_invalid_runtime_reference(
        self,
        snapshot: RuntimeConfigSnapshot,
        **runtime_overrides,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            insert_runtime_config_row(db_path, snapshot, **runtime_overrides)
            before = decision_storage_rows(db_path)

            with self.assertRaises(ValueError):
                store.save_decision_context(decision_context(snapshot))

            after = decision_storage_rows(db_path)
        self.assertEqual(after, before)
        self.assertEqual(len(after[0]), 1)
        self.assertEqual(after[1], [])

    def test_runtime_config_snapshot_round_trip_is_json_safe_and_independent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            snapshot = runtime_config_snapshot(
                {"threshold": 81.5, "guard": {"enabled": True}},
                strategy_build_id="build-7",
            )

            store.save_runtime_config_snapshot(snapshot)
            first = store.load_runtime_config_snapshot(snapshot.hash)
            second = store.load_runtime_config_snapshot(snapshot.hash)

        self.assertIsNotNone(first)
        self.assertEqual(
            set(first),
            {
                "runtime_config_hash",
                "context_version",
                "strategy_build_id",
                "canonical_payload",
                "payload_bytes",
                "created_at_ms",
            },
        )
        self.assertEqual(first["runtime_config_hash"], snapshot.hash)
        self.assertEqual(first["context_version"], CONTEXT_VERSION)
        self.assertEqual(first["strategy_build_id"], "build-7")
        self.assertEqual(first["canonical_payload"], snapshot.canonical_payload)
        self.assertEqual(
            first["payload_bytes"],
            len(snapshot.canonical_payload.encode("utf-8")),
        )
        self.assertIs(type(first["created_at_ms"]), int)
        self.assertEqual(json.loads(json.dumps(first, allow_nan=False)), first)
        self.assertEqual(second, first)
        self.assertIsNot(second, first)
        first["strategy_build_id"] = "mutated"
        self.assertEqual(second["strategy_build_id"], "build-7")

    def test_runtime_config_repeated_save_keeps_one_unchanged_row(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            snapshot = runtime_config_snapshot(
                {"threshold": 81.5},
                strategy_build_id="build-7",
            )

            store.save_runtime_config_snapshot(snapshot)
            original = store.load_runtime_config_snapshot(snapshot.hash)
            store.save_runtime_config_snapshot(snapshot)
            repeated = store.load_runtime_config_snapshot(snapshot.hash)
            with closing(sqlite3.connect(db_path)) as connection:
                count = connection.execute(
                    "select count(*) from runtime_config_snapshots"
                ).fetchone()[0]

        self.assertEqual(count, 1)
        self.assertEqual(repeated, original)

    def test_runtime_config_same_hash_preserves_first_seen_build(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            first_store = SQLiteMonitorStore(db_path)
            second_store = SQLiteMonitorStore(db_path)
            first = runtime_config_snapshot(
                {"threshold": 81.5},
                strategy_build_id="build-a",
            )
            second = runtime_config_snapshot(
                {"threshold": 81.5},
                strategy_build_id="build-b",
            )

            first_store.save_runtime_config_snapshot(first)
            second_store.save_runtime_config_snapshot(second)
            second_build_context = decision_context(second)
            second_store.save_decision_context(second_build_context)
            restored = second_store.load_runtime_config_snapshot(first.hash)
            restored_context = second_store.load_decision_context(
                second_build_context.symbol,
                second_build_context.decision_id,
            )
            with closing(sqlite3.connect(db_path)) as connection:
                count = connection.execute(
                    "select count(*) from runtime_config_snapshots"
                ).fetchone()[0]

        self.assertEqual(first.hash, second.hash)
        self.assertEqual(count, 1)
        self.assertEqual(restored["strategy_build_id"], "build-a")
        self.assertEqual(restored_context, second_build_context.to_dict())
        self.assertEqual(restored_context["strategy_build_id"], "build-b")

    def test_runtime_config_rejects_exact_credential_keys_recursively(self):
        credential_payloads = {
            "root api key": {"api_key": "plaintext"},
            "nested api secret": {"service": {"api_secret": "plaintext"}},
            "list webhook token": {
                "services": [{"enabled": True}, {"webhook_token": "plaintext"}]
            },
            "deep webhook url": {
                "services": [{"notifications": [{"webhook_url": "https://secret"}]}]
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)

            for label, payload in credential_payloads.items():
                with self.subTest(label=label):
                    canonical_payload = json.dumps(
                        payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    snapshot = snapshot_with_payload(canonical_payload)

                    with self.assertRaises(ValueError):
                        store.save_runtime_config_snapshot(snapshot)

                    self.assertEqual(decision_storage_rows(db_path), ([], []))

    def test_runtime_config_credential_boundary_uses_exact_key_semantics(self):
        payload = {
            "apiKey": "allowed",
            "api_key_name": "allowed",
            "notes": [
                "api_secret",
                {"webhook_url_enabled": True},
                {"description": "webhook_token"},
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            snapshot = snapshot_with_payload(json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ))

            store.save_runtime_config_snapshot(snapshot)
            restored = store.load_runtime_config_snapshot(snapshot.hash)

        self.assertEqual(restored["canonical_payload"], snapshot.canonical_payload)

    def test_runtime_config_rejects_malformed_hash_or_payload_without_writing(self):
        payloads = {
            "malformed hash": snapshot_with_payload(
                '{"threshold":81.5}',
                runtime_config_hash="A" * 64,
            ),
            "noncanonical json": snapshot_with_payload('{"b":2, "a":1}'),
            "nan": snapshot_with_payload('{"threshold":NaN}'),
            "invalid json": snapshot_with_payload("{"),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)

            for label, snapshot in payloads.items():
                with self.subTest(label=label):
                    with self.assertRaises((TypeError, ValueError)):
                        store.save_runtime_config_snapshot(snapshot)
                    with closing(sqlite3.connect(db_path)) as connection:
                        count = connection.execute(
                            "select count(*) from runtime_config_snapshots"
                        ).fetchone()[0]
                    self.assertEqual(count, 0)

    def test_runtime_config_rejects_injected_collision_without_overwriting(self):
        valid = runtime_config_snapshot(
            {"threshold": 81.5},
            strategy_build_id="build-7",
        )
        conflicts = {
            "payload": {
                "context_version": CONTEXT_VERSION,
                "canonical_payload": '{"threshold":80.0}',
                "payload_bytes": len('{"threshold":80.0}'.encode("utf-8")),
            },
            "version": {
                "context_version": "DECISION_CONTEXT_V1",
                "canonical_payload": valid.canonical_payload,
                "payload_bytes": len(valid.canonical_payload.encode("utf-8")),
            },
            "bytes": {
                "context_version": CONTEXT_VERSION,
                "canonical_payload": valid.canonical_payload,
                "payload_bytes": 1,
            },
        }
        for label, conflict in conflicts.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                db_path = Path(temp_dir) / "monitor.sqlite3"
                store = SQLiteMonitorStore(db_path)
                injected = (
                    valid.hash,
                    conflict["context_version"],
                    "injected-build",
                    conflict["canonical_payload"],
                    conflict["payload_bytes"],
                    123,
                )
                with closing(sqlite3.connect(db_path)) as connection:
                    connection.execute(
                        """
                        insert into runtime_config_snapshots(
                            runtime_config_hash, context_version, strategy_build_id,
                            canonical_payload, payload_bytes, created_at_ms
                        ) values (?, ?, ?, ?, ?, ?)
                        """,
                        injected,
                    )
                    connection.commit()

                with self.assertRaises(ValueError):
                    store.save_runtime_config_snapshot(valid)

                with closing(sqlite3.connect(db_path)) as connection:
                    persisted = connection.execute(
                        """
                        select runtime_config_hash, context_version, strategy_build_id,
                               canonical_payload, payload_bytes, created_at_ms
                        from runtime_config_snapshots
                        """
                    ).fetchone()
                self.assertEqual(persisted, injected)

    def test_decision_context_round_trip_matches_context_dict(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            snapshot = runtime_config_snapshot(
                {"threshold": 81.5},
                strategy_build_id="build-7",
            )
            context = decision_context(snapshot)

            store.save_runtime_config_snapshot(snapshot)
            store.save_decision_context(context)
            restored = store.load_decision_context(
                context.symbol,
                context.decision_id,
            )
            with closing(sqlite3.connect(db_path)) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    "select * from decision_contexts"
                ).fetchone()

        self.assertEqual(restored, context.to_dict())
        self.assertEqual(json.loads(json.dumps(restored, allow_nan=False)), restored)
        self.assertEqual(row["created_at_ms"], context.closed_kline_at_ms)
        self.assertEqual(row["direction"], "LONG")
        self.assertEqual(row["profile_key"], "profile-a")
        self.assertEqual(row["candidate_origin"], context.candidate_origin)
        self.assertEqual(row["input_payload"], json.dumps(
            context.to_dict()["inputs"],
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ))
        self.assertEqual(
            set(json.loads(row["outcome_payload"])),
            {
                "decision_trace",
                "first_decisive_block",
                "final_decision",
                "final_reason",
                "open_allowed",
                "observation_allowed",
                "selected_order_terms",
            },
        )

    def test_decision_context_repeated_save_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            snapshot = runtime_config_snapshot({"threshold": 81.5})
            context = decision_context(snapshot)
            store.save_runtime_config_snapshot(snapshot)

            store.save_decision_context(context)
            with closing(sqlite3.connect(db_path)) as connection:
                original = connection.execute(
                    "select * from decision_contexts"
                ).fetchone()
            store.save_decision_context(context)
            with closing(sqlite3.connect(db_path)) as connection:
                repeated = connection.execute(
                    "select * from decision_contexts"
                ).fetchone()
                count = connection.execute(
                    "select count(*) from decision_contexts"
                ).fetchone()[0]

        self.assertEqual(count, 1)
        self.assertEqual(repeated, original)

    def test_decision_context_rejects_changed_frozen_data_without_overwriting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            snapshot = runtime_config_snapshot(
                {"threshold": 81.5},
                strategy_build_id="build-7",
            )
            context = decision_context(snapshot)
            store.save_runtime_config_snapshot(snapshot)
            store.save_decision_context(context)
            with closing(sqlite3.connect(db_path)) as connection:
                original = connection.execute(
                    "select * from decision_contexts"
                ).fetchone()

            variants = {
                "input": decision_context(
                    snapshot,
                    inputs={
                        "identity": {
                            "direction": "LONG",
                            "profile_key": "profile-a",
                        },
                        "market": {"close": 101.0},
                    },
                ),
                "outcome": decision_context(snapshot, final_reason="rejected"),
                "metadata": decision_context(
                    snapshot,
                    candidate_origin="manual-replay",
                ),
            }
            for label, changed in variants.items():
                with self.subTest(label=label):
                    with self.assertRaises(ValueError):
                        store.save_decision_context(changed)
                    with closing(sqlite3.connect(db_path)) as connection:
                        persisted = connection.execute(
                            "select * from decision_contexts"
                        ).fetchone()
                        count = connection.execute(
                            "select count(*) from decision_contexts"
                        ).fetchone()[0]
                    self.assertEqual(count, 1)
                    self.assertEqual(persisted, original)

    def test_decision_context_rejects_missing_or_wrong_version_config_reference(self):
        snapshot = runtime_config_snapshot({"threshold": 81.5})
        for label in ("missing", "wrong version"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                db_path = Path(temp_dir) / "monitor.sqlite3"
                store = SQLiteMonitorStore(db_path)
                if label == "wrong version":
                    with closing(sqlite3.connect(db_path)) as connection:
                        connection.execute(
                            """
                            insert into runtime_config_snapshots(
                                runtime_config_hash, context_version,
                                strategy_build_id, canonical_payload,
                                payload_bytes, created_at_ms
                            ) values (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                snapshot.hash,
                                "DECISION_CONTEXT_V1",
                                snapshot.strategy_build_id,
                                snapshot.canonical_payload,
                                len(snapshot.canonical_payload.encode("utf-8")),
                                123,
                            ),
                        )
                        connection.commit()

                with self.assertRaises(ValueError):
                    store.save_decision_context(decision_context(snapshot))

                with closing(sqlite3.connect(db_path)) as connection:
                    count = connection.execute(
                        "select count(*) from decision_contexts"
                    ).fetchone()[0]
                self.assertEqual(count, 0)

    def test_decision_context_save_rejects_runtime_payload_hash_mismatch(self):
        snapshot = runtime_config_snapshot({"threshold": 81.5})
        mismatched_payload = '{"threshold":80.0}'

        self.assert_save_rejects_invalid_runtime_reference(
            snapshot,
            canonical_payload=mismatched_payload,
            payload_bytes=len(mismatched_payload.encode("utf-8")),
        )

    def test_decision_context_save_rejects_runtime_payload_byte_mismatch(self):
        snapshot = runtime_config_snapshot({"threshold": 81.5})

        self.assert_save_rejects_invalid_runtime_reference(
            snapshot,
            payload_bytes=1,
        )

    def test_decision_context_save_rejects_malformed_or_noncanonical_runtime_json(self):
        for label, payload in {
            "malformed": "{",
            "noncanonical": '{"threshold": 81.5}',
        }.items():
            with self.subTest(label=label):
                self.assert_save_rejects_invalid_runtime_reference(
                    snapshot_with_payload(payload),
                )

    def test_decision_context_load_rejects_corrupt_runtime_reference_without_changes(self):
        corruptions = {
            "payload": ("canonical_payload", '{"threshold":80.0}'),
            "hash": ("runtime_config_hash", "A" * 64),
            "bytes": ("payload_bytes", 1),
            "version": ("context_version", "DECISION_CONTEXT_V1"),
            "timestamp": ("created_at_ms", -1),
        }
        for label, (column, value) in corruptions.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                db_path = Path(temp_dir) / "monitor.sqlite3"
                store = SQLiteMonitorStore(db_path)
                snapshot = runtime_config_snapshot({"threshold": 81.5})
                context = decision_context(snapshot)
                store.save_runtime_config_snapshot(snapshot)
                store.save_decision_context(context)
                self.assertEqual(
                    store.load_decision_context(context.symbol, context.decision_id),
                    context.to_dict(),
                )

                with closing(sqlite3.connect(db_path)) as connection:
                    connection.execute(
                        f"update runtime_config_snapshots set {column} = ?",
                        (value,),
                    )
                    if column == "runtime_config_hash":
                        connection.execute(
                            "update decision_contexts set runtime_config_hash = ?",
                            (value,),
                        )
                    connection.commit()
                before_rejection = decision_storage_rows(db_path)

                with self.assertRaises(ValueError):
                    store.load_decision_context(context.symbol, context.decision_id)

                self.assertEqual(decision_storage_rows(db_path), before_rejection)
                self.assertEqual(len(before_rejection[0]), 1)
                self.assertEqual(len(before_rejection[1]), 1)

    def test_decision_context_load_detects_malformed_stored_json(self):
        snapshot = runtime_config_snapshot({"threshold": 81.5})
        corruptions = {
            "invalid input json": ("input_payload", "{"),
            "input is not an object": ("input_payload", "[]"),
            "outcome is not an object": ("outcome_payload", "[]"),
            "trace is not a list": (
                "outcome_payload",
                json.dumps(
                    {
                        "decision_trace": {},
                        "first_decisive_block": "",
                        "final_decision": "OPEN",
                        "final_reason": "accepted",
                        "open_allowed": True,
                        "observation_allowed": False,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        }
        for label, (column, value) in corruptions.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                db_path = Path(temp_dir) / "monitor.sqlite3"
                store = SQLiteMonitorStore(db_path)
                context = decision_context(snapshot)
                store.save_runtime_config_snapshot(snapshot)
                store.save_decision_context(context)
                with closing(sqlite3.connect(db_path)) as connection:
                    connection.execute(
                        f"update decision_contexts set {column} = ?",
                        (value,),
                    )
                    connection.commit()

                with self.assertRaises(ValueError):
                    store.load_decision_context(
                        context.symbol,
                        context.decision_id,
                    )

    def test_decision_context_rejects_invalid_identity_metadata_without_writing(self):
        snapshot = runtime_config_snapshot({"threshold": 81.5})
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            store.save_runtime_config_snapshot(snapshot)
            invalid = decision_context(
                snapshot,
                inputs={
                    "identity": {
                        "direction": ["LONG"],
                        "profile_key": "profile-a",
                    }
                },
            )

            with self.assertRaises((TypeError, ValueError)):
                store.save_decision_context(invalid)

            with closing(sqlite3.connect(db_path)) as connection:
                count = connection.execute(
                    "select count(*) from decision_contexts"
                ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_decision_context_post_insert_mismatch_rolls_back_transaction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            snapshot = runtime_config_snapshot({"threshold": 81.5})
            context = decision_context(snapshot)
            store.save_runtime_config_snapshot(snapshot)
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    """
                    create trigger mutate_decision_after_insert
                    after insert on decision_contexts
                    begin
                        update decision_contexts
                        set candidate_origin = 'trigger-mutated'
                        where symbol = new.symbol and decision_id = new.decision_id;
                    end
                    """
                )
                connection.commit()

            try:
                with self.assertRaises(ValueError):
                    store.save_decision_context(context)
                runtime_rows, decision_rows = decision_storage_rows(db_path)
            finally:
                with closing(sqlite3.connect(db_path)) as connection:
                    connection.execute("drop trigger mutate_decision_after_insert")
                    connection.commit()

        self.assertEqual(len(runtime_rows), 1)
        self.assertEqual(decision_rows, [])

    def test_two_store_instances_save_snapshot_and_context_concurrently(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            first_store = SQLiteMonitorStore(db_path)
            second_store = SQLiteMonitorStore(db_path)
            first_snapshot = runtime_config_snapshot(
                {"threshold": 81.5},
                strategy_build_id="build-a",
            )
            second_snapshot = runtime_config_snapshot(
                {"threshold": 81.5},
                strategy_build_id="build-b",
            )
            context = decision_context(second_snapshot)
            barrier = threading.Barrier(3)
            errors = []
            error_lock = threading.Lock()

            def persist(store, snapshot):
                try:
                    barrier.wait(timeout=5)
                    store.save_runtime_config_snapshot(snapshot)
                    store.save_decision_context(context)
                except BaseException as error:
                    with error_lock:
                        errors.append(error)

            threads = [
                threading.Thread(target=persist, args=(first_store, first_snapshot)),
                threading.Thread(target=persist, args=(second_store, second_snapshot)),
            ]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=10)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            restored_snapshot = first_store.load_runtime_config_snapshot(
                first_snapshot.hash
            )
            restored_context = second_store.load_decision_context(
                context.symbol,
                context.decision_id,
            )
            runtime_rows, decision_rows = decision_storage_rows(db_path)

        self.assertEqual(len(runtime_rows), 1)
        self.assertEqual(len(decision_rows), 1)
        self.assertIn(restored_snapshot["strategy_build_id"], {"build-a", "build-b"})
        self.assertEqual(restored_context, context.to_dict())

    def test_two_store_instances_persist_decision_context_deterministically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            first_store = SQLiteMonitorStore(db_path)
            second_store = SQLiteMonitorStore(db_path)
            snapshot = runtime_config_snapshot(
                {"threshold": 81.5},
                strategy_build_id="build-7",
            )
            context = decision_context(snapshot)

            first_store.save_runtime_config_snapshot(snapshot)
            first_store.save_decision_context(context)
            second_store.save_runtime_config_snapshot(snapshot)
            second_store.save_decision_context(context)
            first = first_store.load_decision_context(
                context.symbol,
                context.decision_id,
            )
            second = second_store.load_decision_context(
                context.symbol,
                context.decision_id,
            )
            with closing(sqlite3.connect(db_path)) as connection:
                counts = connection.execute(
                    """
                    select
                        (select count(*) from runtime_config_snapshots),
                        (select count(*) from decision_contexts)
                    """
                ).fetchone()

        self.assertEqual(counts, (1, 1))
        self.assertEqual(first, context.to_dict())
        self.assertEqual(second, first)

    def test_persists_and_restores_wave_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            snapshot = WaveSnapshot(
                state="UP_LEG",
                raw_state="UP_LEG",
                window=8,
                efficiency=0.9,
                direction_ratio=0.85,
                atr_strength=1.7,
                range_position=0.92,
                confirmations=2,
                confirmed_at=960_000,
                allowed_directions=("LONG",),
            )

            store.save_wave_runtime("BTCUSDT", snapshot, evaluated_at=24_000_000)
            restored = store.load_wave_runtime("BTCUSDT")

        self.assertIsNotNone(restored)
        self.assertEqual(restored["evaluated_at"], 24_000_000)
        self.assertEqual(restored["snapshot"], snapshot)

    def test_persists_and_restores_simulated_orders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            simulator = AccountSimulator()
            order = simulator.open_order(signal(), entry_price=100.0, opened_at=1_000)
            simulator.settle_expired_orders(current_time=601_000, current_price=101.0)

            store.save_order(order, symbol="BTCUSDT")
            restored = store.load_orders("BTCUSDT")

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].id, order.id)
        self.assertEqual(restored[0].status, "SETTLED")
        self.assertEqual(restored[0].result, "WIN")
        self.assertEqual(restored[0].pnl, 8.0)
        self.assertEqual(restored[0].calculated_threshold, 81.5)

    def test_persists_direction_pulse_shadow_on_orders_and_observations(self):
        shadow = {
            "version": "DIRECTION_PULSE_V1_SHADOW",
            "mode": "SHADOW_ONLY",
            "direction": "SHORT",
            "order_slot": "SECOND",
            "windows": {"12": {"status": "WATCH", "would_block": True}},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            saved_order = progression_order(1)
            saved_order.direction_pulse_shadow = shadow
            saved_observation = observation("pulse-shadow")
            saved_observation.direction_pulse_shadow = shadow

            store.save_order(saved_order, "BTCUSDT")
            store.save_observation(saved_observation, "BTCUSDT")
            restored_order = store.load_orders("BTCUSDT")[0]
            restored_observation = store.load_observations("BTCUSDT")[0]

        self.assertEqual(restored_order.direction_pulse_shadow, shadow)
        self.assertEqual(restored_observation.direction_pulse_shadow, shadow)

    def test_saves_observation_settlements_in_one_batch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            observations = [observation(f"batch-{index}") for index in range(3)]

            store.save_observations(observations, "BTCUSDT")
            restored = store.load_observations("BTCUSDT")

        self.assertEqual({item.observation_key for item in restored}, {
            "batch-0",
            "batch-1",
            "batch-2",
        })

    def test_observation_settlement_rejects_every_frozen_field_change(self):
        mutations = {
            "direction": lambda item: replace(item, direction="SHORT"),
            "strategy_family": lambda item: replace(item, strategy_family="MUTATED"),
            "strategy_tag": lambda item: replace(item, strategy_tag="MUTATED"),
            "threshold_segment": lambda item: replace(item, threshold_segment="WD-23"),
            "opened_at": lambda item: replace(item, opened_at=item.opened_at + 1),
            "decision_id": lambda item: replace(item, decision_id="MUTATED"),
            "profile_key": lambda item: replace(item, profile_key="MUTATED"),
            "candidate_origin": lambda item: replace(item, candidate_origin="MUTATED"),
            "entry_price": lambda item: replace(item, entry_price=item.entry_price + 1),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
                opened = replace(
                    observation(
                        "frozen-observation",
                        family="drop_reclaim",
                        tag="live",
                        direction="LONG",
                        segment="WD-08",
                        status="OPEN",
                        result=None,
                    ),
                    profile_key="10|drop_reclaim|live|LONG|WD-08",
                    decision_id="decision-observation",
                    context_version=CONTEXT_VERSION,
                    runtime_config_hash="a" * 64,
                    strategy_build_id="build-observation",
                    candidate_origin="RESEARCH_OBSERVATION",
                )
                store.save_observation(opened, "BTCUSDT")
                settled = replace(
                    opened,
                    status="SETTLED",
                    result="WIN",
                    settled_at=opened.expires_at,
                    exit_price=opened.entry_price + 1,
                    pnl=8.0,
                )

                with self.assertRaisesRegex(ValueError, "frozen observation"):
                    store.save_observation(mutate(settled), "BTCUSDT")

                self.assertEqual(store.load_observations("BTCUSDT"), [opened])

    def test_observation_settlement_updates_only_terminal_fields_idempotently(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            opened = replace(
                observation(
                    "valid-settlement",
                    family="drop_reclaim",
                    tag="live",
                    direction="LONG",
                    segment="WD-08",
                    status="OPEN",
                    result=None,
                ),
                profile_key="10|drop_reclaim|live|LONG|WD-08",
                decision_id="decision-observation",
                context_version=CONTEXT_VERSION,
                runtime_config_hash="a" * 64,
                strategy_build_id="build-observation",
                candidate_origin="RESEARCH_OBSERVATION",
            )
            settled = replace(
                opened,
                status="SETTLED",
                result="LOSS",
                settled_at=opened.expires_at,
                exit_price=opened.entry_price - 1,
                pnl=-10.0,
            )

            store.save_observation(opened, "BTCUSDT")
            store.save_observations([settled], "BTCUSDT")
            store.save_observations([settled], "BTCUSDT")

            self.assertEqual(store.load_observations("BTCUSDT"), [settled])

    def test_persists_and_restores_wave_batch_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            saved = progression_order(1)
            saved.wave_state = "DOWN_LEG"
            saved.wave_raw_state = "DOWN_LEG"
            saved.wave_batch_id = "123|DOWN_LEG|SHORT|WD-05|DPS-1"
            saved.wave_guard_mode = "RECOVERY"
            saved.wave_guard_status = "WAVE_RECOVERY_READY"
            saved.wave_guard_reason = "冷却结束，仅允许恢复单"

            store.save_order(saved, "BTCUSDT")
            restored = store.load_orders("BTCUSDT")[0]

        self.assertEqual(restored.wave_state, "DOWN_LEG")
        self.assertEqual(restored.wave_batch_id, saved.wave_batch_id)
        self.assertEqual(restored.wave_guard_mode, "RECOVERY")
        self.assertEqual(restored.wave_guard_status, "WAVE_RECOVERY_READY")
        self.assertEqual(restored.wave_guard_reason, "冷却结束，仅允许恢复单")

    def test_persists_and_restores_profile_degradation_probe_metadata(self):
        self.assertIn(
            "profile_degradation_probe",
            {field.name for field in fields(SimulatedOrder)},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            saved = progression_order(1)
            saved.profile_degradation_probe = True
            saved.profile_degradation_triggered_at = 987_654

            store.save_order(saved, "BTCUSDT")
            restored = store.load_orders("BTCUSDT")[0]

        self.assertTrue(restored.profile_degradation_probe)
        self.assertEqual(restored.profile_degradation_triggered_at, 987_654)

    def test_persists_and_restores_shadow_quality_score_payloads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            scored_signal = replace(
                signal(),
                quality_score=68.5,
                quality_score_version="QS_V1_SHADOW",
                quality_score_mode="SHADOW_ONLY",
                quality_score_context="LONG_FIRST",
                quality_score_components={"wave_state": 6.0},
                quality_score_inputs={"slot": "FIRST"},
            )
            order = AccountSimulator().open_order(
                scored_signal,
                entry_price=100.0,
                opened_at=1_000,
            )
            scored_observation = replace(
                observation("quality-score"),
                quality_score=72.0,
                quality_score_version="QS_V1_SHADOW",
                quality_score_mode="SHADOW_ONLY",
                quality_score_context="SHORT_SECOND",
                quality_score_components={"volume": 5.0},
                quality_score_inputs={"slot": "SECOND"},
            )

            store.save_order(order, "BTCUSDT")
            store.save_observation(scored_observation, "BTCUSDT")
            restored_order = store.load_orders("BTCUSDT")[0]
            restored_observation = store.load_observations("BTCUSDT")[0]

        self.assertEqual(restored_order.quality_score, 68.5)
        self.assertEqual(restored_order.quality_score_context, "LONG_FIRST")
        self.assertEqual(restored_order.quality_score_components["wave_state"], 6.0)
        self.assertEqual(restored_observation.quality_score, 72.0)
        self.assertEqual(restored_observation.quality_score_context, "SHORT_SECOND")
        self.assertEqual(restored_observation.quality_score_inputs["slot"], "SECOND")

    def test_persists_and_restores_profile_health_audit_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            audited_signal = replace(
                signal(),
                profile_health_status="WATCH",
                profile_health_sample_size=12,
                profile_health_win_rate=0.5,
                profile_health_ev=-1.0,
                profile_health_evaluated_at=1_723_689_600_000,
            )
            order = AccountSimulator().open_order(audited_signal, 100.0, 1_000)
            audited_observation = replace(
                observation("profile-health"),
                profile_health_status="DEGRADED",
                profile_health_sample_size=15,
                profile_health_win_rate=0.466667,
                profile_health_ev=-1.6,
                profile_health_evaluated_at=1_723_689_600_000,
            )

            store.save_order(order, "BTCUSDT")
            store.save_observation(audited_observation, "BTCUSDT")
            restored_order = store.load_orders("BTCUSDT")[0]
            restored_observation = store.load_observations("BTCUSDT")[0]

        self.assertEqual(restored_order.profile_health_status, "WATCH")
        self.assertEqual(restored_order.profile_health_sample_size, 12)
        self.assertEqual(restored_order.profile_health_ev, -1.0)
        self.assertEqual(restored_observation.profile_health_status, "DEGRADED")
        self.assertEqual(restored_observation.profile_health_sample_size, 15)
        self.assertEqual(restored_observation.profile_health_win_rate, 0.466667)

    def test_cancels_multiple_pending_credits_atomically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            first = StakeProgressionCredit(source_order_id=1, created_at=1_000)
            second = StakeProgressionCredit(source_order_id=2, created_at=2_000)
            store.save_stake_progression_credit("BTCUSDT", first)
            store.save_stake_progression_credit("BTCUSDT", second)
            missing = StakeProgressionCredit(source_order_id=3, created_at=3_000, status="CANCELLED")

            with self.assertRaises(ValueError):
                store.cancel_stake_progression_credits(
                    "BTCUSDT",
                    [replace(first, status="CANCELLED"), missing],
                )
            unchanged = store.load_stake_progression_credits("BTCUSDT")
            self.assertEqual([item.status for item in unchanged], ["PENDING", "PENDING"])

            store.cancel_stake_progression_credits(
                "BTCUSDT",
                [replace(first, status="CANCELLED"), replace(second, status="CANCELLED")],
            )
            cancelled = store.load_stake_progression_credits("BTCUSDT")

        self.assertEqual([item.status for item in cancelled], ["CANCELLED", "CANCELLED"])

    def test_persists_signal_audit_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")

            store.save_signal("BTCUSDT", signal(), decision="OPENED", created_at_ms=1_234)
            rows = store.load_recent_signals("BTCUSDT", limit=5)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "BTCUSDT")
        self.assertEqual(rows[0]["decision"], "OPENED")
        self.assertEqual(rows[0]["direction"], "LONG")
        self.assertEqual(rows[0]["regime"], "FEAR_RISING")

    def test_signal_audit_summary_groups_guard_hits_by_profile_and_slot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            guarded = replace(
                signal(),
                profile_key="10|drop_reclaim|long_observe|LONG|WD-08",
                daily_profile_version="DPS-1",
                order_slot="SECOND",
            )
            store.save_signal(
                "BTCUSDT",
                guarded,
                decision="RESULT_SEQUENCE_GUARD_BLOCKED",
                created_at_ms=1_234,
                audit_context={
                    "result_sequence_guard": {"status": "COOLDOWN"},
                    "profile_degradation_guard": {"status": "NORMAL"},
                },
            )
            store.save_signal(
                "BTCUSDT",
                guarded,
                decision="OPENED",
                created_at_ms=2_234,
                audit_context={
                    "result_sequence_guard": {"status": "NORMAL"},
                    "profile_degradation_guard": {"status": "RECOVERY_READY"},
                },
            )

            summary = store.signal_audit_summary("BTCUSDT")

        self.assertEqual(summary["sample_count"], 2)
        decisions = {item["key"]: item["count"] for item in summary["by_decision"]}
        self.assertEqual(decisions["RESULT_SEQUENCE_GUARD_BLOCKED"], 1)
        self.assertEqual(decisions["OPENED"], 1)
        contexts = {item["key"]: item for item in summary["by_profile_dps_slot"]}
        context = contexts[
            "10|drop_reclaim|long_observe|LONG|WD-08|DPS-1|SECOND"
        ]
        self.assertEqual(context["signals"], 2)
        self.assertEqual(context["blocked"], 1)
        sequence_status = {
            item["key"]: item["count"]
            for item in summary["by_result_sequence_status"]
        }
        self.assertEqual(sequence_status["COOLDOWN"], 1)
        self.assertEqual(sequence_status["NORMAL"], 1)

    def test_signal_audit_summary_normalizes_legacy_three_part_profile_key(self):
        complete_key = "10|drop_reclaim|long_observe|LONG|WD-08"
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            guarded = replace(
                signal(),
                strategy_family="drop_reclaim",
                strategy_tag="long_observe",
                threshold_segment="WD-08",
                profile_key="drop_reclaim|LONG|WD-08",
                daily_profile_version="DPS-1",
                order_slot="FIRST",
            )
            store.save_signal(
                "BTCUSDT",
                guarded,
                decision="OPENED",
                created_at_ms=1_234,
            )

            summary = store.signal_audit_summary("BTCUSDT")

        contexts = {item["key"]: item for item in summary["by_profile_dps_slot"]}
        self.assertEqual(
            contexts[f"{complete_key}|DPS-1|FIRST"]["signals"],
            1,
        )
        self.assertNotIn(
            "drop_reclaim|LONG|WD-08|DPS-1|FIRST",
            contexts,
        )

    def test_signal_audit_v2_aggregates_only_identical_ordinary_heartbeats(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            base = replace(
                signal(direction="WAIT"),
                score=11.0,
                profile_key="10|volume_price|wait|WAIT|WD-12",
                context_version="DECISION_CONTEXT_V2",
                runtime_config_hash="a" * 64,
                first_decisive_block="SCORE",
                quality_score_inputs={"must_not_repeat": "quality-inputs"},
                direction_pulse_shadow={"must_not_repeat": "pulse"},
            )
            audit = {
                "result_sequence_guard": {"status": "NORMAL", "history": [1, 2, 3]},
                "profile_degradation_guard": {"status": "NORMAL"},
                "profile_guard": {"full_config": "must-not-repeat"},
            }

            self.assertTrue(
                store.save_signal(
                    "BTCUSDT",
                    base,
                    decision="BELOW_THRESHOLD",
                    created_at_ms=60_000,
                    audit_context=audit,
                    has_formal_candidate=False,
                )
            )
            self.assertTrue(
                store.save_signal(
                    "BTCUSDT",
                    replace(base, score=19.0, rsi=41.0),
                    decision="BELOW_THRESHOLD",
                    created_at_ms=120_000,
                    audit_context=audit,
                    has_formal_candidate=False,
                )
            )
            with store._connect() as connection:
                rows = connection.execute(
                    "select * from signal_audit order by id"
                ).fetchall()

            recent = store.load_recent_signals("BTCUSDT", limit=5)

        self.assertEqual(len(rows), 1)
        row = dict(rows[0])
        self.assertEqual(row["record_version"], "SIGNAL_AUDIT_V2")
        self.assertEqual(row["event_kind"], "HEARTBEAT")
        self.assertEqual(row["occurrences"], 2)
        self.assertEqual((row["first_at_ms"], row["last_at_ms"]), (60_000, 120_000))
        self.assertEqual((row["score_min"], row["score_max"]), (11.0, 19.0))
        payload = json.loads(row["payload"])
        self.assertEqual(payload["metrics"]["score"], 19.0)
        self.assertEqual(payload["metrics"]["rsi"], 41.0)
        self.assertNotIn("quality-inputs", row["payload"])
        self.assertNotIn("must-not-repeat", row["payload"])
        self.assertEqual(recent[0]["occurrences"], 2)
        self.assertEqual(recent[0]["last_at_ms"], 120_000)

    def test_signal_audit_v2_separates_bucket_guard_state_and_observation_candidates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            wait_signal = replace(
                signal(direction="WAIT"),
                profile_key="10|volume_price|wait|WAIT|WD-12",
                context_version="DECISION_CONTEXT_V2",
                runtime_config_hash="b" * 64,
                first_decisive_block="SCORE",
            )
            normal = {"result_sequence_guard": {"status": "NORMAL"}}
            cooldown = {"result_sequence_guard": {"status": "COOLDOWN"}}

            calls = (
                (60_000, normal, False, False, None),
                (600_000, normal, False, False, None),
                (660_000, cooldown, False, False, None),
                (720_000, cooldown, False, True, "OBSERVATION_CANDIDATE"),
                (780_000, cooldown, True, False, None),
            )
            for created_at_ms, audit, has_formal, force_independent, event_kind in calls:
                store.save_signal(
                    "BTCUSDT",
                    wait_signal,
                    decision="BELOW_THRESHOLD",
                    created_at_ms=created_at_ms,
                    audit_context=audit,
                    has_formal_candidate=has_formal,
                    force_independent=force_independent,
                    event_kind=event_kind,
                )
            with store._connect() as connection:
                rows = connection.execute(
                    "select aggregation_key, occurrences, event_kind from signal_audit order by id"
                ).fetchall()

        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row["occurrences"] == 1 for row in rows))
        self.assertIsNone(rows[2]["aggregation_key"])
        self.assertEqual(rows[2]["event_kind"], "STATE_CHANGE")
        self.assertIsNone(rows[-2]["aggregation_key"])
        self.assertIsNone(rows[-1]["aggregation_key"])
        self.assertEqual(rows[-2]["event_kind"], "OBSERVATION_CANDIDATE")
        self.assertEqual(rows[-1]["event_kind"], "DECISION")

    def test_signal_audit_v2_regime_change_is_a_state_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            base = replace(
                signal(direction="WAIT"),
                profile_key="10|volume_price|wait|WAIT|WD-12",
                context_version="DECISION_CONTEXT_V2",
                runtime_config_hash="d" * 64,
                regime="FEAR_RISING",
            )
            store.save_signal(
                "BTCUSDT",
                base,
                decision="WAIT",
                created_at_ms=60_000,
            )
            store.save_signal(
                "BTCUSDT",
                replace(base, regime="GREED_FALLING"),
                decision="WAIT",
                created_at_ms=120_000,
            )
            with store._connect() as connection:
                rows = connection.execute(
                    "select regime, event_kind, aggregation_key from signal_audit order by id"
                ).fetchall()

        self.assertEqual([row["regime"] for row in rows], ["FEAR_RISING", "GREED_FALLING"])
        self.assertEqual(rows[1]["event_kind"], "STATE_CHANGE")
        self.assertIsNone(rows[1]["aggregation_key"])

    def test_signal_audit_v2_has_one_complete_compact_guard_snapshot(self):
        audit = {
            "result_sequence_guard": {
                "status": "PAUSED", "code": "RESULT_SEQUENCE_GUARD_BLOCKED",
                "blocked": True, "scope": "DIRECTION", "direction": "LONG",
                "consecutive_losses": 3, "last_settled_at": 100, "pause_until": 200,
                "history": ["must-not-repeat"],
            },
            "wave_batch_guard": {
                "status": "BATCH_LOCKED", "code": "WAVE_BATCH_LOSS_LOCKED",
                "mode": "BATCH_LOCKED", "blocked": True, "allow_progression": False,
                "current_batch_id": "batch-1", "batch_orders": 2, "batch_wins": 0,
                "batch_losses": 1, "failed_batches": 2, "pause_until": 300,
                "config": {"must-not-repeat": True},
            },
            "profile_degradation_guard": {
                "status": "COOLDOWN", "code": "PROFILE_DEGRADATION_BLOCKED",
                "blocked": True, "allow_progression": False, "profile_key": "profile-a",
                "daily_profile_version": "DPS-1", "consecutive_losses": 3,
                "last_loss_settled_at": 400, "pause_until": 500,
                "probe_order_id": 7, "triggered_at": 400,
            },
            "profile_health_guard": {
                "status": "DEGRADED", "code": "PROFILE_HEALTH_BLOCKED",
                "blocked": True, "direction": "LONG", "evaluated_at": 600,
                "next_evaluation_at": 700, "sample_size": 12, "wins": 4,
                "losses": 8, "win_rate": 0.333333, "pnl": -48.0, "ev": -4.0,
                "allow_second_order": False, "allow_progression": False,
            },
            "rolling_edge": {
                "status": "DEGRADED", "code": "ROLLING_EDGE_BLOCKED",
                "sample_size": 6, "wins": 2, "losses": 4, "win_rate": 0.3333,
                "pnl": -24.0, "ev": -4.0, "blocked": True,
                "key": "10|WD-12|rebound-long", "edge": 10.0, "threshold": 0.5,
            },
            "time_period_guard": {
                "enabled": True, "blocked": True, "code": "TIME_PERIOD_SHADOW_ONLY",
                "local_hour": 14, "window": "12:00-18:00",
            },
            "profile_guard": {
                "status": "WOULD_BLOCK", "code": "PROFILE_GUARD_BLOCKED",
                "enabled": True, "observe_only": False, "blocked": True,
                "hit_keys": ["risk-a"], "full_config": "must-not-repeat",
            },
        }
        pulse = {
            "version": "DIRECTION_PULSE_V1_SHADOW",
            "mode": "SHADOW_ONLY",
            "direction": "LONG",
            "order_slot": "FIRST",
            "evaluated_at": 800,
            "windows": {
                "12": {"status": "WATCH", "hypothetical_action": "BLOCK_SECOND"},
                "16": {"status": "NORMAL", "hypothetical_action": "ALLOW"},
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_signal(
                "BTCUSDT",
                replace(
                    signal(),
                    risk_flags="RISK_A",
                    direction_pulse_shadow=pulse,
                    quality_score_inputs={"must-not-repeat": True},
                ),
                decision="PROFILE_HEALTH_BLOCKED",
                created_at_ms=1_000,
                audit_context=audit,
                force_independent=True,
            )
            with store._connect() as connection:
                row = connection.execute(
                    "select payload from signal_audit"
                ).fetchone()

        raw_payload = row["payload"]
        payload = json.loads(raw_payload)
        self.assertIn("guards", payload)
        self.assertNotIn("guards", payload["state_code"])
        self.assertEqual(len(payload["state_code"]["guard_state_hash"]), 64)
        self.assertEqual(payload["state_code"]["regime"], "FEAR_RISING")
        self.assertEqual(payload["state_code"]["risk_flags"], "RISK_A")
        self.assertEqual(payload["guards"]["result_sequence"]["scope"], "DIRECTION")
        self.assertEqual(payload["guards"]["result_sequence"]["pause_until"], 200)
        self.assertEqual(payload["guards"]["wave_batch"]["batch_losses"], 1)
        self.assertEqual(payload["guards"]["profile_degradation"]["probe_order_id"], 7)
        self.assertEqual(payload["guards"]["profile_health"]["allow_second_order"], False)
        self.assertEqual(payload["guards"]["rolling_edge"]["threshold"], 0.5)
        self.assertEqual(
            payload["guards"]["rolling_edge"]["key"],
            "10|WD-12|rebound-long",
        )
        self.assertEqual(payload["guards"]["time_period"]["local_hour"], 14)
        self.assertEqual(payload["guards"]["profile_guard"]["hit_keys"], ["risk-a"])
        self.assertEqual(payload["guards"]["wave_signal"]["status"], "UNKNOWN")
        self.assertEqual(payload["direction_pulse"]["status"], "WATCH")
        self.assertEqual(payload["direction_pulse"]["code"], "BLOCK_SECOND")
        self.assertNotIn("must-not-repeat", raw_payload)
        self.assertNotIn("full_config", raw_payload)
        self.assertNotIn("history", raw_payload)
        self.assertNotIn("quality_score_inputs", raw_payload)
        self.assertLess(len(raw_payload.encode("utf-8")), 5_000)

    def test_wave_signal_uses_stable_status_as_code_not_human_reason(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_signal(
                "BTCUSDT",
                replace(
                    signal(),
                    wave_guard_mode="DIRECTION_BLOCKED",
                    wave_guard_status="DIRECTION_BLOCKED",
                    wave_guard_reason="一分钟波段方向不允许开多",
                ),
                decision="WAVE_DIRECTION_BLOCKED",
                created_at_ms=1_000,
                force_independent=True,
            )
            with store._connect() as connection:
                row = connection.execute("select payload from signal_audit").fetchone()

        wave_signal = json.loads(row["payload"])["guards"]["wave_signal"]
        self.assertEqual(wave_signal["mode"], "DIRECTION_BLOCKED")
        self.assertEqual(wave_signal["status"], "DIRECTION_BLOCKED")
        self.assertEqual(wave_signal["code"], "DIRECTION_BLOCKED")
        self.assertNotIn("一分钟", wave_signal["code"])

    def test_signal_audit_summary_weights_v2_occurrences_and_legacy_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            with store._connect() as connection:
                connection.execute(
                    """
                    insert into signal_audit(
                        symbol, created_at_ms, decision, direction, timeframe_minutes,
                        threshold_segment, regime, score, threshold, reason, payload
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "BTCUSDT", 1, "WAIT", "WAIT", 10, "WD-12", "UNKNOWN",
                        0.0, 70.0, "legacy", json.dumps({"profile_key": "legacy"}),
                    ),
                )
            heartbeat = replace(
                signal(direction="WAIT"),
                profile_key="v2-profile",
                context_version="DECISION_CONTEXT_V2",
                runtime_config_hash="c" * 64,
            )
            for created_at_ms in range(10_000, 19_000, 1_000):
                store.save_signal(
                    "BTCUSDT",
                    heartbeat,
                    decision="WAIT",
                    created_at_ms=created_at_ms,
                    has_formal_candidate=False,
                )

            summary = store.signal_audit_summary("BTCUSDT")

        self.assertEqual(summary["sample_count"], 10)
        self.assertEqual(summary["storage_rows"], 2)
        decisions = {item["key"]: item["count"] for item in summary["by_decision"]}
        self.assertEqual(decisions["WAIT"], 10)
        contexts = {item["key"]: item for item in summary["by_profile_dps_slot"]}
        self.assertEqual(contexts["v2-profile|STATIC|UNKNOWN"]["signals"], 9)

    def test_compact_only_skips_ordinary_heartbeat_but_keeps_core_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            compact = capacity_for_bytes(COMPACT_ONLY_BYTES)
            with mock.patch("app.storage.capacity_from_connection", return_value=compact):
                skipped = store.save_signal(
                    "BTCUSDT",
                    signal(direction="WAIT"),
                    decision="WAIT",
                    created_at_ms=1_000,
                    has_formal_candidate=False,
                )
                saved = store.save_signal(
                    "BTCUSDT",
                    signal(),
                    decision="OPENED",
                    created_at_ms=2_000,
                    has_formal_candidate=True,
                )
            rows = store.load_recent_signals("BTCUSDT", limit=10)

        self.assertFalse(skipped)
        self.assertTrue(saved)
        self.assertEqual([row["decision"] for row in rows], ["OPENED"])

    def test_two_store_instances_aggregate_same_heartbeat_concurrently(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "monitor.sqlite3"
            stores = [SQLiteMonitorStore(path), SQLiteMonitorStore(path)]
            barrier = threading.Barrier(2)
            failures = []

            def save(store):
                try:
                    barrier.wait(timeout=5)
                    store.save_signal(
                        "BTCUSDT",
                        replace(signal(direction="WAIT"), regime="FEAR_FLAT"),
                        decision="WAIT",
                        created_at_ms=60_000,
                    )
                except Exception as error:  # noqa: BLE001 - capture thread evidence.
                    failures.append(error)

            threads = [threading.Thread(target=save, args=(store,)) for store in stores]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            rows = stores[0].load_recent_signals("BTCUSDT", limit=10)

        self.assertEqual(failures, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["occurrences"], 2)

    def test_save_signal_translates_full_by_write_class_and_preserves_other_errors(self):
        normal_capacity = capacity_for_bytes(0)

        def run(error, *, decision):
            with tempfile.TemporaryDirectory() as temp_dir:
                store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
                connection = mock.MagicMock()

                def execute(statement, *args):
                    if "insert into signal_audit" in " ".join(statement.split()).lower():
                        raise error
                    cursor = mock.MagicMock()
                    cursor.fetchone.return_value = None
                    return cursor

                connection.execute.side_effect = execute

                @contextmanager
                def failing_connect():
                    yield connection

                with mock.patch.object(store, "_connect", failing_connect), mock.patch(
                    "app.storage.capacity_from_connection", return_value=normal_capacity
                ):
                    return store.save_signal(
                        "BTCUSDT",
                        signal(direction="WAIT" if decision == "WAIT" else "LONG"),
                        decision=decision,
                        created_at_ms=1_000,
                    )

        self.assertFalse(run(sqlite3.OperationalError("database or disk is full"), decision="WAIT"))
        with self.assertRaises(CoreStorageCapacityError):
            run(sqlite3.OperationalError("database or disk is full"), decision="OPENED")
        with self.assertRaisesRegex(sqlite3.OperationalError, "syntax error"):
            run(sqlite3.OperationalError("syntax error"), decision="WAIT")

    def test_save_signal_handles_real_sqlite_page_exhaustion_without_partial_rows(self):
        normal_capacity = capacity_for_bytes(0)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(path)
            with closing(sqlite3.connect(path)) as connection:
                page_count = connection.execute("pragma page_count").fetchone()[0]
            page_limit = page_count + 1

            def configure_tiny_page_limit(connection):
                return connection.execute(
                    f"pragma max_page_count = {page_limit}"
                ).fetchone()[0]

            with mock.patch(
                "app.storage.configure_max_page_count",
                side_effect=configure_tiny_page_limit,
            ), mock.patch(
                "app.storage.capacity_from_connection",
                return_value=normal_capacity,
            ):
                ordinary_result = True
                attempts = 0
                while ordinary_result and attempts < 100:
                    attempts += 1
                    ordinary_result = store.save_signal(
                        "BTCUSDT",
                        replace(signal(direction="WAIT"), reason="x" * 1_500),
                        decision="WAIT",
                        created_at_ms=attempts * 600_000,
                    )

                with closing(sqlite3.connect(path)) as connection:
                    rows_before_core = connection.execute(
                        "select count(*) from signal_audit"
                    ).fetchone()[0]
                with self.assertRaises(CoreStorageCapacityError):
                    store.save_signal(
                        "BTCUSDT",
                        replace(signal(), reason="y" * 1_500),
                        decision="OPENED",
                        created_at_ms=(attempts + 1) * 600_000,
                        force_independent=True,
                    )
                with closing(sqlite3.connect(path)) as connection:
                    rows_after_core = connection.execute(
                        "select count(*) from signal_audit"
                    ).fetchone()[0]

        self.assertLess(attempts, 100)
        self.assertFalse(ordinary_result)
        self.assertEqual(rows_before_core, attempts - 1)
        self.assertEqual(rows_after_core, rows_before_core)

    def test_persists_order_entry_snapshot_and_updates_settlement(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            simulator = AccountSimulator()
            order = simulator.open_order(signal(), entry_price=100.0, opened_at=1_000)
            entry_snapshot = {
                "signal": signal().to_dict(),
                "rolling_edge": {"status": "NORMAL", "sample_size": 21, "win_rate": 0.619},
                "latest_kline": {"close": 100.0, "close_time": 1_000},
                "stake_config": {"stake": 10.0, "win_return": 18.0},
            }

            store.save_order_entry_snapshot(order, "BTCUSDT", entry_snapshot)
            simulator.settle_expired_orders(current_time=601_000, current_price=99.0)
            store.update_order_entry_snapshot_settlement(order, "BTCUSDT")
            rows = store.load_order_entry_snapshots("BTCUSDT")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "BTCUSDT")
        self.assertEqual(rows[0]["order_id"], 1)
        self.assertEqual(rows[0]["direction"], "LONG")
        self.assertEqual(rows[0]["threshold_segment"], "WD-12")
        self.assertEqual(rows[0]["result"], "LOSS")
        self.assertEqual(rows[0]["pnl"], -10.0)
        self.assertEqual(rows[0]["entry_payload"]["rolling_edge"]["sample_size"], 21)
        self.assertEqual(rows[0]["settlement_payload"]["exit_price"], 99.0)

    def test_pages_and_filters_orders_for_dashboard(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            fixtures = [
                SimulatedOrder(
                    id=1,
                    direction="LONG",
                    timeframe_minutes=10,
                    level="S",
                    reason="long win",
                    entry_price=100.0,
                    opened_at=1_000,
                    expires_at=601_000,
                    threshold_segment="WD-08",
                    status="SETTLED",
                    result="WIN",
                    exit_price=101.0,
                    settled_at=601_000,
                    pnl=8.0,
                ),
                SimulatedOrder(
                    id=2,
                    direction="SHORT",
                    timeframe_minutes=10,
                    level="A",
                    reason="short loss",
                    entry_price=100.0,
                    opened_at=2_000,
                    expires_at=602_000,
                    threshold_segment="WD-23",
                    status="SETTLED",
                    result="LOSS",
                    exit_price=101.0,
                    settled_at=602_000,
                    pnl=-10.0,
                ),
                SimulatedOrder(
                    id=3,
                    direction="SHORT",
                    timeframe_minutes=10,
                    level="S",
                    reason="short open",
                    entry_price=100.0,
                    opened_at=3_000,
                    expires_at=603_000,
                    threshold_segment="WD-23",
                    status="OPEN",
                    result=None,
                ),
            ]
            for order in fixtures:
                store.save_order(order, "BTCUSDT")
            for order_id in range(4, 14):
                store.save_order(
                    SimulatedOrder(
                        id=order_id,
                        direction="LONG",
                        timeframe_minutes=10,
                        level="B",
                        reason="page filler",
                        entry_price=100.0,
                        opened_at=order_id * 1_000,
                        expires_at=order_id * 1_000 + 600_000,
                        threshold_segment="WD-12",
                        status="SETTLED",
                        result="WIN",
                        exit_price=101.0,
                        settled_at=order_id * 1_000 + 600_000,
                        pnl=8.0,
                    ),
                    "BTCUSDT",
                )

            first_page = store.page_orders("BTCUSDT", page=1, page_size=10)
            filtered = store.page_orders(
                "BTCUSDT",
                page=1,
                page_size=10,
                direction="SHORT",
                level="S",
                segment="WD-23",
                result="OPEN",
            )

        self.assertEqual(first_page["total"], 13)
        self.assertEqual(first_page["page"], 1)
        self.assertEqual(first_page["page_size"], 10)
        self.assertEqual(first_page["total_pages"], 2)
        self.assertEqual([item["id"] for item in first_page["orders"]], [13, 12, 11, 10, 9, 8, 7, 6, 5, 4])
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["orders"][0]["id"], 3)
        self.assertEqual(filtered["orders"][0]["direction"], "SHORT")

    def test_persists_and_pages_observation_signals(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_observation(
                observation("1|10|SHORT|normal_down_short_extension_observe"),
                "BTCUSDT",
            )

            rows = store.load_observations("BTCUSDT")
            page = store.page_observations(
                "BTCUSDT",
                direction="SHORT",
                family="short_extension",
                tag="normal_down_short_extension_observe",
                segment="WD-02",
                result="WIN",
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].strategy_family, "short_extension")
        self.assertEqual(rows[0].result, "WIN")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["observations"][0]["strategy_tag"], "normal_down_short_extension_observe")

    def test_persists_daily_profile_selection_idempotently_and_loads_effective_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            first = {
                "version": "DPS-1",
                "status": "READY",
                "evaluated_at": 900,
                "effective_from": 1_000,
                "effective_until": 2_000,
                "selected_profiles": [{"key": "10|family|tag|LONG|WD-01"}],
            }
            replacement = {
                **first,
                "version": "DPS-1-REPLACED",
                "selected_profiles": [{"key": "10|family|tag|SHORT|WD-01", "direction": "SHORT"}],
            }
            second = {
                "version": "DPS-2",
                "status": "READY",
                "evaluated_at": 1_900,
                "effective_from": 2_000,
                "effective_until": 3_000,
                "selected_profiles": [],
            }

            store.save_daily_profile_selection("BTCUSDT", first)
            store.save_daily_profile_selection("BTCUSDT", replacement)
            store.save_daily_profile_selection("BTCUSDT", second)

            active_first = store.load_daily_profile_selection("BTCUSDT", 1_500)
            active_second = store.load_daily_profile_selection("BTCUSDT", 2_500)
            latest = store.load_latest_daily_profile_selection("BTCUSDT")
            missing = store.load_daily_profile_selection("ETHUSDT", 1_500)

        self.assertEqual(active_first["version"], "DPS-1-REPLACED")
        self.assertEqual(active_first["selected_profiles"][0]["direction"], "SHORT")
        self.assertEqual(active_second["version"], "DPS-2")
        self.assertEqual(latest["version"], "DPS-2")
        self.assertIsNone(missing)

    def test_loads_daily_profile_snapshot_as_of_evaluation_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")

            def snapshot(version, evaluation_key):
                return {
                    "version": version,
                    "status": "READY",
                    "evaluated_at": evaluation_key + 5,
                    "evaluation_key": evaluation_key,
                    "lookback_end": evaluation_key,
                    "effective_from": evaluation_key + 10 * 60_000,
                    "effective_until": evaluation_key + 86_400_000,
                    "selected_profiles": [{"key": version}],
                }

            store.save_daily_profile_selection("BTCUSDT", snapshot("PAST", 1_000))
            store.save_daily_profile_selection("BTCUSDT", snapshot("CURRENT", 2_000))
            store.save_daily_profile_selection("BTCUSDT", snapshot("FUTURE", 3_000))
            future_same_evaluation = snapshot("FUTURE-SAME-EVALUATION", 2_000)
            future_same_evaluation["evaluated_at"] = 2_500
            future_same_evaluation["effective_from"] += 1
            future_same_evaluation["effective_until"] += 1
            store.save_daily_profile_selection("BTCUSDT", future_same_evaluation)

            current = store.load_daily_profile_selection_as_of(
                "BTCUSDT",
                2_000,
                evaluated_at_ms=2_300,
            )
            between = store.load_daily_profile_selection_as_of("BTCUSDT", 1_500)
            before_all = store.load_daily_profile_selection_as_of("BTCUSDT", 999)
            latest = store.load_latest_daily_profile_selection("BTCUSDT")
            store.close()

        self.assertEqual(current["version"], "CURRENT")
        self.assertEqual(between["version"], "PAST")
        self.assertIsNone(before_all)
        self.assertEqual(latest["version"], "FUTURE")

    def test_as_of_loader_migrates_legacy_rows_using_payload_lookback_end(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            payload = {
                "version": "LEGACY",
                "status": "READY",
                "evaluated_at": 1_500,
                "lookback_end": 1_000,
                "effective_from": 2_000,
                "effective_until": 3_000,
                "selected_profiles": [],
            }
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    """
                    create table daily_profile_selections (
                        symbol text not null,
                        effective_from integer not null,
                        effective_until integer not null,
                        status text not null,
                        evaluated_at integer not null,
                        payload text not null,
                        updated_at_ms integer not null default 0,
                        primary key(symbol, effective_from)
                    )
                    """
                )
                connection.execute(
                    "insert into daily_profile_selections values (?, ?, ?, ?, ?, ?, ?)",
                    ("BTCUSDT", 2_000, 3_000, "READY", 1_500, json.dumps(payload), 0),
                )

            store = SQLiteMonitorStore(db_path)
            restored = store.load_daily_profile_selection_as_of("BTCUSDT", 1_000)
            with sqlite3.connect(db_path) as connection:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "pragma table_info(daily_profile_selections)"
                    )
                }
            store.close()

        self.assertEqual(restored["version"], "LEGACY")
        self.assertIn("evaluation_key", columns)

    def test_loads_complete_latest_observation_profile_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            for idx in range(650):
                store.save_observation(
                    observation(
                        f"profile-{idx}",
                        result="LOSS" if idx < 150 else "WIN",
                        opened_at=idx * 600_000,
                    ),
                    "BTCUSDT",
                )

            rows = store.load_observations_for_profile("BTCUSDT", lookback_days=7)

        self.assertEqual(len(rows), 650)
        self.assertEqual(sum(item.result == "LOSS" for item in rows), 150)

    def test_profile_restore_loads_one_day_buffer_for_scheduled_daily_cutoff(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            latest = 9 * 86_400_000
            store.save_observation(observation("latest", opened_at=latest), "BTCUSDT")
            store.save_observation(
                observation("cutoff-buffer", opened_at=latest - 7 * 86_400_000 - 12 * 60 * 60_000),
                "BTCUSDT",
            )
            store.save_observation(
                observation("too-old", opened_at=latest - 8 * 86_400_000 - 60_000),
                "BTCUSDT",
            )

            rows = store.load_observations_for_profile("BTCUSDT", lookback_days=7)

        self.assertEqual({item.observation_key for item in rows}, {"latest", "cutoff-buffer"})

    def test_summarizes_observation_signals_by_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            for idx in range(30):
                store.save_observation(
                    observation(
                        f"promote-{idx}",
                        result="WIN" if idx < 24 else "LOSS",
                        opened_at=idx * 1_000,
                    ),
                    "BTCUSDT",
                )
            for idx in range(35):
                store.save_observation(
                    observation(
                        f"block-{idx}",
                        family="low_rise_observe",
                        tag="low_volume_rise_observe",
                        direction="LONG",
                        segment="WD-18",
                        result="WIN" if idx < 15 else "LOSS",
                        opened_at=100_000 + idx * 1_000,
                    ),
                    "BTCUSDT",
                )
            store.save_observation(
                observation("open-1", status="OPEN", result=None, opened_at=200_000),
                "BTCUSDT",
            )

            summary = store.observation_summary("BTCUSDT")

        self.assertEqual(summary["total"]["signals"], 66)
        self.assertEqual(summary["total"]["settled"], 65)
        self.assertEqual(summary["total"]["open"], 1)
        self.assertEqual(summary["action_counts"]["PROMOTE_WATCH"], 1)
        self.assertEqual(summary["action_counts"]["BLOCK_WATCH"], 1)
        promote = next(item for item in summary["groups"] if item["strategy_family"] == "short_extension")
        blocked = next(item for item in summary["groups"] if item["strategy_family"] == "low_rise_observe")
        self.assertEqual(promote["direction"], "SHORT")
        self.assertEqual(promote["settled"], 30)
        self.assertEqual(promote["wins"], 24)
        self.assertEqual(promote["action"], "PROMOTE_WATCH")
        self.assertEqual(promote["confidence"], "MEDIUM")
        self.assertEqual(blocked["action"], "BLOCK_WATCH")
        self.assertLess(blocked["ev"], 0)

    def test_observation_queries_cover_rows_beyond_legacy_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_observations(
                [
                    observation(
                        f"row-{idx:04d}",
                        result="WIN" if idx % 2 == 0 else "LOSS",
                        opened_at=idx * 1_000,
                    )
                    for idx in range(5_101)
                ],
                "BTCUSDT",
            )

            summary = store.observation_summary("BTCUSDT", window="all")
            last_page = store.page_observations(
                "BTCUSDT", page=256, page_size=20
            )

        self.assertEqual(summary["total"]["settled"], 5_101)
        self.assertEqual(summary["total"]["signals"], 5_101)
        self.assertEqual(last_page["total"], 5_101)
        self.assertEqual(last_page["page"], 256)
        self.assertEqual(len(last_page["observations"]), 1)
        self.assertEqual(last_page["observations"][0]["observation_key"], "row-0000")

    def test_observation_summary_uses_stable_anchor_and_time_windows(self):
        day = 86_400_000
        anchor = 40 * day
        fixtures = [
            ("anchor", anchor),
            ("seven-edge", anchor - 7 * day),
            ("seven-old", anchor - 7 * day - 1),
            ("fourteen-edge", anchor - 14 * day),
            ("fourteen-old", anchor - 14 * day - 1),
            ("thirty-edge", anchor - 30 * day),
            ("thirty-old", anchor - 30 * day - 1),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_observations(
                [observation(key, opened_at=opened_at) for key, opened_at in fixtures],
                "BTCUSDT",
            )

            seven = store.observation_summary("BTCUSDT", window="7d")
            default = store.observation_summary("BTCUSDT")
            thirty = store.observation_summary("BTCUSDT", window="30d")
            all_rows = store.observation_summary("BTCUSDT", window="all")

        self.assertEqual(seven["total"]["signals"], 2)
        self.assertEqual(default["window"], "14d")
        self.assertEqual(default["total"]["signals"], 4)
        self.assertEqual(thirty["total"]["signals"], 6)
        self.assertEqual(all_rows["total"]["signals"], 7)
        self.assertEqual(seven["anchor"], anchor)
        self.assertEqual(seven["cutoff"], anchor - 7 * day)

    def test_observation_summary_keeps_legacy_positional_limit_with_keyword_window(self):
        day = 86_400_000
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_observations(
                [
                    observation("latest", opened_at=40 * day),
                    observation("old", opened_at=10 * day),
                ],
                "BTCUSDT",
            )

            legacy_default = store.observation_summary("BTCUSDT", 5_000)
            explicit_all = store.observation_summary(
                "BTCUSDT", 5_000, window="all"
            )

        self.assertEqual(legacy_default["window"], "14d")
        self.assertEqual(legacy_default["total"]["signals"], 1)
        self.assertEqual(explicit_all["window"], "all")
        self.assertEqual(explicit_all["total"]["signals"], 2)

    def test_observation_summary_preserves_zero_pnl_result_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_observations(
                [
                    replace(observation("win", result="WIN"), pnl=0.0),
                    replace(observation("loss", result="LOSS", opened_at=2_000), pnl=0.0),
                ],
                "BTCUSDT",
            )

            summary = store.observation_summary("BTCUSDT", window="all")

        self.assertEqual(summary["total"]["pnl"], -2.0)
        self.assertEqual(summary["total"]["ev"], -1.0)

    def test_observation_sql_filters_and_options_use_the_full_symbol_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_observations(
                [
                    observation(f"base-{idx}", opened_at=idx)
                    for idx in range(5_005)
                ]
                + [
                    observation(
                        "rare",
                        family="rare_family",
                        tag="rare_tag",
                        direction="LONG",
                        segment="WE-23",
                        status="OPEN",
                        result=None,
                        opened_at=-1,
                    )
                ],
                "BTCUSDT",
            )

            page = store.page_observations(
                "BTCUSDT",
                page=1,
                page_size=10,
                direction="LONG",
                family="rare_family",
                tag="rare_tag",
                segment="WE-23",
                result="OPEN",
            )

        self.assertEqual(page["total"], 1)
        self.assertEqual(page["observations"][0]["observation_key"], "rare")
        self.assertIn("RARE_FAMILY", page["filter_options"]["family"])
        self.assertIn("RARE_TAG", page["filter_options"]["tag"])
        self.assertIn("WE-23", page["filter_options"]["segment"])

    def test_summarizes_order_entry_snapshots_for_weak_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            for order_id in (1, 2):
                order = SimulatedOrder(
                    id=order_id,
                    direction="LONG",
                    timeframe_minutes=10,
                    level="A",
                    reason="放量急跌反抽：synthetic",
                    entry_price=100.0,
                    opened_at=order_id * 1_000,
                    expires_at=order_id * 1_000 + 600_000,
                    threshold_segment="WD-18",
                    score=85.0,
                    threshold=70.0,
                    status="SETTLED",
                    result="LOSS",
                    exit_price=99.0,
                    settled_at=order_id * 1_000 + 600_000,
                    pnl=-10.0,
                )
                entry_signal = signal()
                entry_signal = entry_signal.__class__(
                    **{
                        **entry_signal.to_dict(),
                        "level": "A",
                        "reason": "放量急跌反抽：synthetic",
                        "threshold_segment": "WD-18",
                        "price_change_pct": -0.0015,
                        "price_position": 0.45,
                        "rsi": 46.0,
                        "mtf_10m_bias": 0.1,
                        "mtf_30m_bias": 0.2,
                        "regime": "FEAR_FALLING",
                    }
                )
                store.save_order_entry_snapshot(
                    order,
                    "BTCUSDT",
                    {
                        "signal": entry_signal.to_dict(),
                        "fear_greed": {"value": 12, "trend": "falling"},
                        "profile_guard_shadow": {
                            "status": "WOULD_BLOCK",
                            "hit_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
                            "active_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
                            "min_history": 15,
                            "min_group_size": 2,
                            "variant": "recommended_key_subset",
                            "selection_policy": {
                                "name": "STABILITY_BAND",
                                "reason": "最高稳定分组合已满足稳定带",
                                "selected_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
                                "score_best_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
                            },
                        },
                        "profile_guard_selection_policy": {
                            "name": "STABILITY_BAND",
                            "reason": "最高稳定分组合已满足稳定带",
                            "selected_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
                            "score_best_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
                        },
                        "profile_guard_default_shadow": {
                            "status": "PASS",
                            "hit_keys": [],
                            "active_keys": ["HIGH_RSI_REBOUND", "WEAK_SEGMENT_WD00_WD18_WD22"],
                            "min_history": 15,
                            "min_group_size": 2,
                            "variant": "walk_forward_combined",
                        },
                    },
                )
                store.update_order_entry_snapshot_settlement(order, "BTCUSDT")

            summary = store.order_profile_summary("BTCUSDT")
            store.wait_for_profile_summary_rebuilds(timeout=10)
            summary = store.order_profile_summary("BTCUSDT")

        self.assertEqual(summary["total"]["orders"], 2)
        self.assertEqual(summary["total"]["losses"], 2)
        hint_names = {item["key"] for item in summary["risk_hints"]}
        self.assertIn("LEVEL_A_REBOUND", hint_names)
        self.assertIn("WEAK_SEGMENT_WD00_WD18_WD22", hint_names)
        self.assertIn("HIGH_RSI_REBOUND", hint_names)
        self.assertEqual(summary["profile_guard_shadow"]["would_block"]["orders"], 2)
        self.assertEqual(summary["profile_guard_shadow"]["would_block"]["losses"], 2)
        self.assertEqual(summary["profile_guard_policy"]["by_policy"][0]["key"], "STABILITY_BAND")
        self.assertEqual(summary["profile_guard_shadow_compare"]["recommended_block_default_pass"]["orders"], 2)

    def test_upgrades_legacy_database_and_restores_order_progression_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            legacy_order = progression_order(1).to_dict()
            legacy_order["threshold"] = 79.0
            legacy_order.pop("calculated_threshold")
            legacy_order.pop("stake_progression_source_order_id")
            legacy_order.pop("stake_progression_version")
            legacy_order.pop("profile_degradation_probe", None)
            legacy_order.pop("profile_degradation_triggered_at", None)
            for key in list(legacy_order):
                if key.startswith("wave_"):
                    legacy_order.pop(key)
            with closing(sqlite3.connect(db_path)) as connection, connection:
                connection.execute(
                    """
                    create table orders (
                        symbol text not null,
                        order_id integer not null,
                        status text not null,
                        result text,
                        opened_at integer not null,
                        settled_at integer,
                        payload text not null,
                        updated_at_ms integer not null default (strftime('%s','now') * 1000),
                        primary key(symbol, order_id)
                    )
                    """
                )
                connection.execute(
                    """
                    insert into orders(symbol, order_id, status, result, opened_at, settled_at, payload)
                    values (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("BTCUSDT", 1, "OPEN", None, 1_000, None, json.dumps(legacy_order)),
                )

            store = SQLiteMonitorStore(db_path)
            restored = store.load_orders("BTCUSDT")
            with closing(sqlite3.connect(db_path)) as connection, connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "select name from sqlite_master where type = 'table'"
                    )
                }
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        insert into stake_progression_runtime(
                            symbol, version, activated_at, enabled
                        ) values ('INVALID', 'V1', 0, 2)
                        """
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        insert into stake_progression_credits(
                            symbol, version, credit_id, source_order_id, status, created_at
                        ) values ('INVALID', 'V1', 'bad', 1, 'UNKNOWN', 0)
                        """
                    )

        self.assertIn("stake_progression_runtime", tables)
        self.assertIn("stake_progression_credits", tables)
        self.assertEqual(restored[0].stake_progression_source_order_id, None)
        self.assertEqual(restored[0].stake_progression_version, "")
        self.assertEqual(restored[0].wave_state, "UNKNOWN")
        self.assertEqual(restored[0].wave_batch_id, "")
        self.assertEqual(restored[0].wave_guard_mode, "NORMAL")
        self.assertEqual(restored[0].calculated_threshold, 79.0)
        self.assertIs(
            getattr(restored[0], "profile_degradation_probe", None),
            False,
        )
        self.assertEqual(
            getattr(restored[0], "profile_degradation_triggered_at", None),
            0,
        )

    def test_round_trips_all_credit_states_in_stable_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            credits = [
                StakeProgressionCredit(
                    source_order_id=3,
                    created_at=30,
                    credit_id="z-pending",
                    direction="SHORT",
                ),
                StakeProgressionCredit(
                    source_order_id=2,
                    created_at=10,
                    credit_id="b-consumed",
                    status="CONSUMED",
                    consumed_order_id=20,
                    consumed_at=40,
                    direction="LONG",
                ),
                StakeProgressionCredit(
                    source_order_id=1,
                    created_at=10,
                    credit_id="a-cancelled",
                    status="CANCELLED",
                    direction="SHORT",
                ),
            ]
            for credit in credits:
                store.save_stake_progression_credit("BTCUSDT", credit)

            restored = store.load_stake_progression_credits("btcusdt")

        self.assertEqual(
            [credit.credit_id for credit in restored],
            ["a-cancelled", "b-consumed", "z-pending"],
        )
        self.assertEqual(
            [credit.to_dict() for credit in restored],
            [credits[2].to_dict(), credits[1].to_dict(), credits[0].to_dict()],
        )

    def test_upgrades_legacy_progression_credit_table_with_direction_column(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            with closing(sqlite3.connect(db_path)) as connection, connection:
                connection.execute(
                    """
                    create table stake_progression_credits (
                        symbol text not null,
                        version text not null,
                        credit_id text not null,
                        source_order_id integer not null,
                        status text not null,
                        created_at integer not null,
                        consumed_order_id integer,
                        consumed_at integer,
                        primary key(symbol, version, source_order_id),
                        unique(symbol, version, credit_id),
                        unique(symbol, version, consumed_order_id)
                    )
                    """
                )
                connection.execute(
                    """
                    insert into stake_progression_credits(
                        symbol, version, credit_id, source_order_id, status, created_at
                    ) values ('BTCUSDT', ?, 'legacy:1', 1, 'PENDING', 1000)
                    """,
                    (TWO_STAGE_VERSION,),
                )

            store = SQLiteMonitorStore(db_path)
            restored = store.load_stake_progression_credits("BTCUSDT")

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].direction, "")

    def test_prepare_runtime_preserves_activation_boundary_across_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            first = SQLiteMonitorStore(db_path)

            initial = first.prepare_stake_progression(
                "BTCUSDT", TWO_STAGE_VERSION, True, 1_000
            )
            restarted = SQLiteMonitorStore(db_path)
            restored = restarted.prepare_stake_progression(
                "btcusdt", TWO_STAGE_VERSION, True, 9_000
            )

        self.assertEqual(initial, 1_000)
        self.assertEqual(restored, 1_000)

    def test_disable_then_enable_cancels_pending_and_sets_new_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.prepare_stake_progression("BTCUSDT", TWO_STAGE_VERSION, True, 1_000)
            store.save_stake_progression_credit(
                "BTCUSDT",
                StakeProgressionCredit(source_order_id=1, created_at=1_100),
            )

            disabled_at = store.prepare_stake_progression(
                "BTCUSDT", TWO_STAGE_VERSION, False, 2_000
            )
            disabled_again_at = store.prepare_stake_progression(
                "BTCUSDT", TWO_STAGE_VERSION, False, 3_000
            )
            enabled_at = store.prepare_stake_progression(
                "BTCUSDT", TWO_STAGE_VERSION, True, 4_000
            )
            restored = store.load_stake_progression_credits("BTCUSDT")

        self.assertEqual((disabled_at, disabled_again_at, enabled_at), (1_000, 1_000, 4_000))
        self.assertEqual(restored[0].status, "CANCELLED")

    def test_version_change_cancels_all_pending_and_replaces_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.prepare_stake_progression("BTCUSDT", TWO_STAGE_VERSION, True, 1_000)
            store.save_stake_progression_credit(
                "BTCUSDT",
                StakeProgressionCredit(source_order_id=1, created_at=1_100),
            )

            activated_at = store.prepare_stake_progression(
                "BTCUSDT", "TWO_STAGE_V2", True, 2_000
            )
            old_credits = store.load_stake_progression_credits(
                "BTCUSDT", TWO_STAGE_VERSION
            )

        self.assertEqual(activated_at, 2_000)
        self.assertEqual(old_credits[0].status, "CANCELLED")

    def test_prepare_and_cancellation_are_isolated_by_symbol(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            for symbol, source_order_id in (("BTCUSDT", 1), ("ETHUSDT", 2)):
                store.prepare_stake_progression(symbol, TWO_STAGE_VERSION, True, 1_000)
                store.save_stake_progression_credit(
                    symbol,
                    StakeProgressionCredit(
                        source_order_id=source_order_id,
                        created_at=1_100,
                    ),
                )

            store.prepare_stake_progression("BTCUSDT", TWO_STAGE_VERSION, False, 2_000)
            btc = store.load_stake_progression_credits("BTCUSDT")
            eth = store.load_stake_progression_credits("ETHUSDT")
            eth_activation = store.prepare_stake_progression(
                "ETHUSDT", TWO_STAGE_VERSION, True, 9_000
            )

        self.assertEqual(btc[0].status, "CANCELLED")
        self.assertEqual(eth[0].status, "PENDING")
        self.assertEqual(eth_activation, 1_000)

    def test_prepare_rejects_invalid_runtime_parameters(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            invalid = [
                ("", TWO_STAGE_VERSION, True, 0),
                ("BTCUSDT", " ", True, 0),
                ("BTCUSDT", TWO_STAGE_VERSION, True, -1),
            ]
            for args in invalid:
                with self.subTest(args=args), self.assertRaises(ValueError):
                    store.prepare_stake_progression(*args)

    def test_saves_settled_order_and_credit_atomically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            order = progression_order(1, status="SETTLED")
            credit = StakeProgressionCredit(
                source_order_id=order.id,
                created_at=order.settled_at,
            )
            store.save_order(
                replace(
                    order,
                    status="OPEN",
                    result=None,
                    settled_at=None,
                    exit_price=None,
                    pnl=0.0,
                ),
                "BTCUSDT",
            )

            store.save_settled_order_with_credit(order, "BTCUSDT", credit)
            store.save_settled_order_with_credit(order, "BTCUSDT", credit)
            orders = store.load_orders("BTCUSDT")
            credits = store.load_stake_progression_credits("BTCUSDT")

        self.assertEqual([item.id for item in orders], [1])
        self.assertEqual([item.to_dict() for item in credits], [credit.to_dict()])

    def test_saves_open_second_stage_order_and_consumption_idempotently(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            pending = StakeProgressionCredit(source_order_id=1, created_at=1_000)
            consumed = StakeProgressionCredit(
                source_order_id=1,
                created_at=1_000,
                status="CONSUMED",
                consumed_order_id=2,
                consumed_at=2_000,
            )
            order = progression_order(2, step=2, source_order_id=1)
            store.save_stake_progression_credit("BTCUSDT", pending)

            store.save_open_order_with_credit(order, "BTCUSDT", consumed)
            store.save_open_order_with_credit(order, "BTCUSDT", consumed)
            orders = store.load_orders("BTCUSDT")
            credits = store.load_stake_progression_credits("BTCUSDT")

        self.assertEqual([item.id for item in orders], [2])
        self.assertEqual(credits[0].status, "CONSUMED")
        self.assertEqual(credits[0].consumed_order_id, 2)

    def test_atomic_methods_reject_invalid_credit_links_without_partial_orders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            settled_order = progression_order(10, status="SETTLED")
            invalid_settlements = [
                StakeProgressionCredit(source_order_id=11, created_at=1_000),
                StakeProgressionCredit(
                    source_order_id=10,
                    created_at=1_000,
                    status="CONSUMED",
                    consumed_order_id=20,
                    consumed_at=2_000,
                ),
                StakeProgressionCredit(
                    source_order_id=10,
                    created_at=1_000,
                    version="OTHER_VERSION",
                ),
            ]
            for index, credit in enumerate(invalid_settlements):
                symbol = f"SETTLED{index}"
                with self.subTest(kind="settled", index=index), self.assertRaises(ValueError):
                    store.save_settled_order_with_credit(settled_order, symbol, credit)
                self.assertEqual(store.load_orders(symbol), [])

            open_order = progression_order(20, step=2, source_order_id=10)
            invalid_opens = [
                StakeProgressionCredit(source_order_id=10, created_at=1_000),
                StakeProgressionCredit(
                    source_order_id=10,
                    created_at=1_000,
                    status="CONSUMED",
                    consumed_order_id=21,
                    consumed_at=2_000,
                ),
                StakeProgressionCredit(
                    source_order_id=11,
                    created_at=1_000,
                    status="CONSUMED",
                    consumed_order_id=20,
                    consumed_at=2_000,
                ),
                StakeProgressionCredit(
                    source_order_id=10,
                    created_at=1_000,
                    version="OTHER_VERSION",
                    status="CONSUMED",
                    consumed_order_id=20,
                    consumed_at=2_000,
                ),
                None,
            ]
            for index, credit in enumerate(invalid_opens):
                symbol = f"OPEN{index}"
                with self.subTest(kind="open", index=index), self.assertRaises(ValueError):
                    store.save_open_order_with_credit(open_order, symbol, credit)
                self.assertEqual(store.load_orders(symbol), [])

            base_order = progression_order(30, version="")
            store.save_open_order_with_credit(base_order, "BASE", None)
            base_orders = store.load_orders("BASE")

        self.assertEqual(base_orders[0].id, 30)

    def test_credit_upsert_failure_rolls_back_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            order = progression_order(1, status="SETTLED")
            credit = StakeProgressionCredit(source_order_id=1, created_at=601_000)
            open_order = replace(
                order,
                status="OPEN",
                result=None,
                settled_at=None,
                exit_price=None,
                pnl=0.0,
            )
            store.save_order(open_order, "BTCUSDT")

            with mock.patch.object(
                store,
                "_upsert_progression_credit",
                side_effect=RuntimeError("credit write failed"),
                create=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "credit write failed"):
                    store.save_settled_order_with_credit(order, "BTCUSDT", credit)

            self.assertEqual(store.load_orders("BTCUSDT"), [open_order])

    def test_atomic_methods_reject_cross_direction_credit_links(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            settled_long = progression_order(1, status="SETTLED")
            wrong_settlement_credit = StakeProgressionCredit(
                source_order_id=settled_long.id,
                created_at=settled_long.settled_at,
                direction="SHORT",
            )

            with self.assertRaisesRegex(ValueError, "direction"):
                store.save_settled_order_with_credit(
                    settled_long,
                    "BTCUSDT",
                    wrong_settlement_credit,
                )

            pending = StakeProgressionCredit(
                source_order_id=10,
                created_at=1_000,
                direction="LONG",
            )
            consumed = replace(
                pending,
                status="CONSUMED",
                consumed_order_id=2,
                consumed_at=2_000,
            )
            short_order = replace(
                progression_order(2, step=2, source_order_id=10),
                direction="SHORT",
            )
            store.save_stake_progression_credit("BTCUSDT", pending)

            with self.assertRaisesRegex(ValueError, "direction"):
                store.save_open_order_with_credit(short_order, "BTCUSDT", consumed)
            restored = store.load_stake_progression_credits("BTCUSDT")

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].status, "PENDING")
        self.assertEqual(restored[0].direction, "LONG")

    def test_settlement_credit_rejects_second_stage_order_without_partial_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            order = progression_order(
                2,
                status="SETTLED",
                step=2,
                source_order_id=1,
            )
            credit = StakeProgressionCredit(source_order_id=2, created_at=601_000)

            with self.assertRaises(ValueError):
                store.save_settled_order_with_credit(order, "BTCUSDT", credit)

            self.assertEqual(store.load_orders("BTCUSDT"), [])
            self.assertEqual(store.load_stake_progression_credits("BTCUSDT"), [])

    def test_open_credit_rejects_base_order_without_partial_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            order = progression_order(2, step=1, source_order_id=1)
            credit = StakeProgressionCredit(
                source_order_id=1,
                created_at=1_000,
                status="CONSUMED",
                consumed_order_id=2,
                consumed_at=2_000,
            )

            with self.assertRaises(ValueError):
                store.save_open_order_with_credit(order, "BTCUSDT", credit)

            self.assertEqual(store.load_orders("BTCUSDT"), [])
            self.assertEqual(store.load_stake_progression_credits("BTCUSDT"), [])

    def test_consumed_credit_rejects_competing_order_without_partial_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            original = StakeProgressionCredit(
                source_order_id=1,
                created_at=1_000,
                credit_id="credit-1",
                status="CONSUMED",
                consumed_order_id=2,
                consumed_at=2_000,
            )
            store.save_open_order_with_credit(
                progression_order(2, step=2, source_order_id=1),
                "BTCUSDT",
                original,
            )
            competing = StakeProgressionCredit(
                source_order_id=1,
                created_at=1_000,
                credit_id="credit-1",
                status="CONSUMED",
                consumed_order_id=3,
                consumed_at=3_000,
            )

            with self.assertRaises(ValueError):
                store.save_open_order_with_credit(
                    progression_order(3, step=2, source_order_id=1),
                    "BTCUSDT",
                    competing,
                )

            orders = store.load_orders("BTCUSDT")
            credits = store.load_stake_progression_credits("BTCUSDT")

        self.assertEqual([order.id for order in orders], [2])
        self.assertEqual(credits[0].to_dict(), original.to_dict())

    def test_cancelled_credit_rejects_consumption_without_partial_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            cancelled = StakeProgressionCredit(
                source_order_id=1,
                created_at=1_000,
                credit_id="credit-1",
                status="CANCELLED",
            )
            store.save_stake_progression_credit("BTCUSDT", cancelled)
            competing = StakeProgressionCredit(
                source_order_id=1,
                created_at=1_000,
                credit_id="credit-1",
                status="CONSUMED",
                consumed_order_id=3,
                consumed_at=3_000,
            )

            with self.assertRaises(ValueError):
                store.save_open_order_with_credit(
                    progression_order(3, step=2, source_order_id=1),
                    "BTCUSDT",
                    competing,
                )

            self.assertEqual(store.load_orders("BTCUSDT"), [])
            credits = store.load_stake_progression_credits("BTCUSDT")

        self.assertEqual(credits[0].to_dict(), cancelled.to_dict())

    def test_late_settlement_pending_snapshot_preserves_consumed_credit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            consumed = StakeProgressionCredit(
                source_order_id=1,
                created_at=601_000,
                status="CONSUMED",
                consumed_order_id=2,
                consumed_at=602_000,
            )
            store.save_stake_progression_credit("BTCUSDT", consumed)
            late_pending = StakeProgressionCredit(
                source_order_id=1,
                created_at=601_000,
            )
            settled_order = progression_order(1, status="SETTLED")
            store.save_order(
                replace(
                    settled_order,
                    status="OPEN",
                    result=None,
                    settled_at=None,
                    exit_price=None,
                    pnl=0.0,
                ),
                "BTCUSDT",
            )

            store.save_settled_order_with_credit(
                settled_order,
                "BTCUSDT",
                late_pending,
            )
            orders = store.load_orders("BTCUSDT")
            credits = store.load_stake_progression_credits("BTCUSDT")

        self.assertEqual([order.id for order in orders], [1])
        self.assertEqual(credits[0].to_dict(), consumed.to_dict())

    def test_late_pending_snapshot_cannot_downgrade_consumed_credit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            consumed = StakeProgressionCredit(
                source_order_id=1,
                created_at=1_000,
                status="CONSUMED",
                consumed_order_id=2,
                consumed_at=2_000,
            )
            late_pending = StakeProgressionCredit(
                source_order_id=1,
                created_at=1_000,
            )

            store.save_stake_progression_credit("BTCUSDT", consumed)
            store.save_stake_progression_credit("BTCUSDT", late_pending)
            restored = store.load_stake_progression_credits("BTCUSDT")

        self.assertEqual(restored[0].status, "CONSUMED")
        self.assertEqual(restored[0].consumed_order_id, 2)
        self.assertEqual(restored[0].consumed_at, 2_000)

    def test_credit_terminal_states_cannot_transition(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            consumed = StakeProgressionCredit(
                source_order_id=1,
                created_at=1_000,
                status="CONSUMED",
                consumed_order_id=2,
                consumed_at=2_000,
            )
            cancelled = StakeProgressionCredit(
                source_order_id=1,
                created_at=1_000,
                status="CANCELLED",
            )
            store.save_stake_progression_credit("CONSUMED", consumed)
            store.save_stake_progression_credit("CONSUMED", cancelled)
            store.save_stake_progression_credit("CANCELLED", cancelled)
            store.save_stake_progression_credit("CANCELLED", consumed)

            still_consumed = store.load_stake_progression_credits("CONSUMED")
            still_cancelled = store.load_stake_progression_credits("CANCELLED")

        self.assertEqual(still_consumed[0].status, "CONSUMED")
        self.assertEqual(still_cancelled[0].status, "CANCELLED")


class AtomicDecisionBundleTest(unittest.TestCase):
    def test_orders_schema_has_unique_non_null_decision_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            store.close()

            with closing(sqlite3.connect(db_path)) as connection:
                indexes = connection.execute("pragma index_list(orders)").fetchall()
                matching = [
                    row for row in indexes
                    if row[1] == "ux_orders_symbol_decision_id"
                ]
                sql = connection.execute(
                    "select sql from sqlite_master where type = 'index' and name = ?",
                    ("ux_orders_symbol_decision_id",),
                ).fetchone()

            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0][2], 1)
            self.assertIn("where decision_id is not null", sql[0].lower())

    def test_legacy_database_without_decision_index_is_upgraded_in_place(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            store.close()
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute("drop index ux_orders_symbol_decision_id")
                connection.execute("pragma user_version = 2")
                connection.commit()

            upgraded = SQLiteMonitorStore(db_path)
            upgraded.close()

            with closing(sqlite3.connect(db_path)) as connection:
                index = connection.execute(
                    "select sql from sqlite_master where type = 'index' and name = ?",
                    ("ux_orders_symbol_decision_id",),
                ).fetchone()
            self.assertIsNotNone(index)
            self.assertIn("where decision_id is not null", index[0].lower())

    def test_legacy_duplicate_decision_bindings_require_explicit_repair(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            store.close()
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute("drop index ux_orders_symbol_decision_id")
                for order_id in (1, 2):
                    connection.execute(
                        """
                        insert into orders(
                            symbol, order_id, status, opened_at, payload,
                            decision_id, runtime_config_hash
                        ) values (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "BTCUSDT",
                            order_id,
                            "OPEN",
                            1_000,
                            "{}",
                            "duplicate-decision",
                            "config-hash",
                        ),
                    )
                connection.execute("pragma user_version = 2")
                connection.commit()

            with self.assertRaisesRegex(RuntimeError, "duplicate decision_id"):
                SQLiteMonitorStore(db_path)

            with closing(sqlite3.connect(db_path)) as connection:
                count = connection.execute(
                    "select count(*) from orders where decision_id = ?",
                    ("duplicate-decision",),
                ).fetchone()[0]
                index = connection.execute(
                    "select 1 from sqlite_master where type = 'index' and name = ?",
                    ("ux_orders_symbol_decision_id",),
                ).fetchone()
            self.assertEqual(count, 2)
            self.assertIsNone(index)

    def test_open_bundle_commits_all_members_with_matching_references(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            config, context, order, audit, entry_snapshot, observed = (
                structured_atomic_bundle()
            )
            observed = replace(
                observed,
                adaptive_profile_state={
                    "qualification_state": "QUALIFIED",
                    "status": "RESIDENT",
                },
            )

            store.save_open_order_decision(
                config=config,
                context=context,
                order=order,
                credit=None,
                entry_snapshot=entry_snapshot,
                audit=audit,
                observation=observed,
            )

            self.assertEqual(
                atomic_bundle_counts(db_path),
                {
                    "runtime_config_snapshots": 1,
                    "decision_contexts": 1,
                    "orders": 1,
                    "stake_progression_credits": 0,
                    "order_entry_snapshots": 1,
                    "signal_audit": 1,
                    "observation_signals": 1,
                },
            )
            with closing(sqlite3.connect(db_path)) as connection:
                order_row = connection.execute(
                    "select decision_id, runtime_config_hash from orders"
                ).fetchone()
                entry_row = connection.execute(
                    "select decision_id, context_version, runtime_config_hash "
                    "from order_entry_snapshots"
                ).fetchone()
                audit_row = connection.execute(
                    "select decision_id, runtime_config_hash, aggregation_key, event_kind "
                    "from signal_audit"
                ).fetchone()
                observation_row = connection.execute(
                    "select decision_id, context_version, runtime_config_hash, candidate_origin, "
                    "qualification_state, adaptive_state, entry_structure_state, "
                    "entry_structure_bias, active_level_source "
                    "from observation_signals"
                ).fetchone()
            expected_pair = (context.decision_id, context.runtime_config_hash)
            self.assertEqual(order_row, expected_pair)
            self.assertEqual(
                entry_row,
                (context.decision_id, context.context_version, context.runtime_config_hash),
            )
            self.assertEqual(
                audit_row,
                (context.decision_id, context.runtime_config_hash, None, "ORDER_OPENED"),
            )
            self.assertEqual(
                observation_row,
                (
                    context.decision_id,
                    context.context_version,
                    context.runtime_config_hash,
                    context.candidate_origin,
                    "QUALIFIED",
                    "RESIDENT",
                    ENTRY_STRUCTURE_FIXTURE["entry_structure_state"],
                    ENTRY_STRUCTURE_FIXTURE["entry_structure_bias"],
                    ENTRY_STRUCTURE_FIXTURE["active_level_source"],
                ),
            )

    def test_open_bundle_is_idempotent_and_rejects_a_changed_collision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            fixture = atomic_bundle_fixture()
            config, context, order, audit, entry_snapshot, observed = fixture
            arguments = {
                "config": config,
                "context": context,
                "order": order,
                "credit": None,
                "entry_snapshot": entry_snapshot,
                "audit": audit,
                "observation": observed,
            }

            store.save_open_order_decision(**arguments)
            store.save_open_order_decision(**arguments)
            unchanged = atomic_bundle_counts(db_path)
            with self.assertRaises(ValueError):
                store.save_open_order_decision(
                    **{**arguments, "order": replace(order, entry_price=101.0)}
                )

            self.assertEqual(atomic_bundle_counts(db_path), unchanged)
            self.assertEqual(unchanged["signal_audit"], 1)

    def test_open_bundle_rejects_same_decision_for_a_different_order_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            config, context, order, audit, entry_snapshot, observed = (
                atomic_bundle_fixture()
            )
            arguments = {
                "config": config,
                "context": context,
                "order": order,
                "credit": None,
                "entry_snapshot": entry_snapshot,
                "audit": audit,
                "observation": observed,
            }
            store.save_open_order_decision(**arguments)

            with self.assertRaisesRegex(ValueError, "decision.*order"):
                store.save_open_order_decision(
                    **{**arguments, "order": replace(order, id=order.id + 1)}
                )

            counts = atomic_bundle_counts(db_path)
            self.assertEqual(counts["orders"], 1)
            self.assertEqual(counts["order_entry_snapshots"], 1)
            self.assertEqual(counts["signal_audit"], 1)
            self.assertEqual(counts["observation_signals"], 1)

    def test_concurrent_different_orders_and_audits_for_one_decision_commit_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            stores = (SQLiteMonitorStore(db_path), SQLiteMonitorStore(db_path))
            config, context, order, audit, entry_snapshot, observed = (
                atomic_bundle_fixture()
            )
            attempts = (
                (order, audit, entry_snapshot),
                (
                    replace(order, id=order.id + 1),
                    replace(
                        audit,
                        signal=replace(
                            audit.signal,
                            reason="competing audit content",
                            score=audit.signal.score + 1,
                        ),
                        audit_context={"result_sequence_guard": {"status": "COMPETING"}},
                    ),
                    {
                        **entry_snapshot,
                        "signal": {
                            **entry_snapshot["signal"],
                            "reason": "competing audit content",
                            "score": audit.signal.score + 1,
                        },
                    },
                ),
            )
            barrier = threading.Barrier(2)
            errors = []

            def save(store, attempted_order, attempted_audit, attempted_snapshot):
                try:
                    barrier.wait(timeout=5)
                    store.save_open_order_decision(
                        config=config,
                        context=context,
                        order=attempted_order,
                        credit=None,
                        entry_snapshot=attempted_snapshot,
                        audit=attempted_audit,
                        observation=observed,
                    )
                except Exception as error:  # noqa: BLE001 - captures competing writer.
                    errors.append(error)

            threads = [
                threading.Thread(target=save, args=(store, *attempt))
                for store, attempt in zip(stores, attempts)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], ValueError)
            self.assertRegex(str(errors[0]), "decision.*order")
            counts = atomic_bundle_counts(db_path)
            self.assertEqual(counts["decision_contexts"], 1)
            self.assertEqual(counts["orders"], 1)
            self.assertEqual(counts["order_entry_snapshots"], 1)
            self.assertEqual(counts["signal_audit"], 1)
            self.assertEqual(counts["observation_signals"], 1)
            with closing(sqlite3.connect(db_path)) as connection:
                winning_order_id = connection.execute(
                    "select order_id from orders"
                ).fetchone()[0]
                persisted_reason = connection.execute(
                    "select reason from signal_audit"
                ).fetchone()[0]
            expected_reason = (
                audit.signal.reason
                if winning_order_id == order.id
                else "competing audit content"
            )
            self.assertEqual(persisted_reason, expected_reason)

    def test_open_bundle_retry_does_not_repeat_credit_consumption_or_members(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            config, context, order, audit, entry_snapshot, observed = (
                atomic_bundle_fixture()
            )
            source_id = 99
            pending = StakeProgressionCredit(
                source_order_id=source_id,
                created_at=100,
                direction=order.direction,
            )
            consumed = StakeProgressionCredit(
                source_order_id=source_id,
                created_at=100,
                consumed_order_id=order.id,
                consumed_at=order.opened_at,
                status="CONSUMED",
                direction=order.direction,
            )
            order = replace(
                order,
                stake_progression_step=2,
                stake_progression_source_order_id=source_id,
                stake_progression_version=TWO_STAGE_VERSION,
            )
            store.save_stake_progression_credit(context.symbol, pending)
            arguments = {
                "config": config,
                "context": context,
                "order": order,
                "credit": consumed,
                "entry_snapshot": entry_snapshot,
                "audit": audit,
                "observation": observed,
            }

            store.save_open_order_decision(**arguments)
            store.save_open_order_decision(**arguments)

            credits = store.load_stake_progression_credits(context.symbol)
            counts = atomic_bundle_counts(db_path)
            self.assertEqual(len(credits), 1)
            self.assertEqual(credits[0].status, "CONSUMED")
            self.assertEqual(credits[0].consumed_order_id, order.id)
            self.assertEqual(counts["orders"], 1)
            self.assertEqual(counts["order_entry_snapshots"], 1)
            self.assertEqual(counts["signal_audit"], 1)
            self.assertEqual(counts["observation_signals"], 1)

    def test_open_bundle_retry_accepts_legacy_full_order_and_observation_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            config, context, order, audit, entry_snapshot, observed = (
                atomic_bundle_fixture()
            )
            arguments = {
                "config": config,
                "context": context,
                "order": order,
                "credit": None,
                "entry_snapshot": entry_snapshot,
                "audit": audit,
                "observation": observed,
            }
            store.save_open_order_decision(**arguments)
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    "update orders set payload = ?",
                    (json.dumps(order.to_dict(), ensure_ascii=False),),
                )
                connection.execute(
                    "update observation_signals set payload = ?",
                    (json.dumps(observed.to_dict(), ensure_ascii=False),),
                )
                connection.commit()

            created = store.save_open_order_decision(**arguments)
            restored_order = store.load_orders(context.symbol)[0]
            restored_observation = store.load_observations(context.symbol)[0]

            self.assertFalse(created)
            self.assertEqual(restored_order.decision_inputs, context.to_dict()["inputs"])
            self.assertEqual(
                restored_observation.decision_inputs,
                context.to_dict()["inputs"],
            )
            settled_order = replace(
                restored_order,
                status="SETTLED",
                result="WIN",
                settled_at=restored_order.expires_at,
                exit_price=restored_order.entry_price + 1.0,
                pnl=8.0,
            )
            settled_observation = replace(
                restored_observation,
                status="SETTLED",
                result="WIN",
                settled_at=restored_observation.expires_at,
                exit_price=restored_observation.entry_price + 1.0,
                pnl=8.0,
            )

            store.save_settled_order_with_credit(
                settled_order,
                context.symbol,
                None,
            )
            store.save_observation(settled_observation, context.symbol)

            self.assertEqual(store.load_orders(context.symbol)[0].status, "SETTLED")
            self.assertEqual(
                store.load_observations(context.symbol)[0].status,
                "SETTLED",
            )

    def test_open_bundle_rejects_frozen_redundant_column_collisions(self):
        cases = (
            (
                "observation qualification",
                "update observation_signals set qualification_state = 'CONFLICT'",
            ),
            (
                "entry score",
                "update order_entry_snapshots set score = score + 1",
            ),
        )
        for label, tamper_sql in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                db_path = Path(temp_dir) / "monitor.sqlite3"
                store = SQLiteMonitorStore(db_path)
                config, context, order, audit, entry_snapshot, observed = (
                    atomic_bundle_fixture()
                )
                observed = replace(
                    observed,
                    adaptive_profile_state={"qualification_state": "QUALIFIED"},
                )
                arguments = {
                    "config": config,
                    "context": context,
                    "order": order,
                    "credit": None,
                    "entry_snapshot": entry_snapshot,
                    "audit": audit,
                    "observation": observed,
                }
                store.save_open_order_decision(**arguments)
                with closing(sqlite3.connect(db_path)) as connection:
                    connection.execute(tamper_sql)
                    connection.commit()

                with self.assertRaisesRegex(ValueError, "frozen decision data"):
                    store.save_open_order_decision(**arguments)

                self.assertEqual(atomic_bundle_counts(db_path)["signal_audit"], 1)

    def test_decision_bundle_commits_optional_observation_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            config, context, _order, audit, _entry_snapshot, observed = (
                atomic_bundle_fixture()
            )
            blocked_context = decision_context(
                config,
                decision_id=context.decision_id,
                closed_kline_at_ms=context.closed_kline_at_ms,
                candidate_origin=context.candidate_origin,
                inputs=context.to_dict()["inputs"],
                decision_trace=(
                    {
                        "stage": "TRANSITIONAL_FINAL",
                        "result": "BLOCK",
                        "decisive_values": {"decision": "PROFILE_BLOCKED"},
                        "reason_code": "PROFILE_BLOCKED",
                    },
                ),
                first_decisive_block="TRANSITIONAL_FINAL",
                final_decision="PROFILE_BLOCKED",
                final_reason="blocked",
                open_allowed=False,
                observation_allowed=True,
            )
            blocked_signal = replace(
                audit.signal,
                decision_trace=blocked_context.to_dict()["decision_trace"],
                first_decisive_block=blocked_context.first_decisive_block,
            )
            blocked_audit = replace(
                audit,
                signal=blocked_signal,
                decision="PROFILE_BLOCKED",
                event_kind="DECISIVE_BLOCK",
            )
            blocked_observation = replace(
                observed,
                source_decision="PROFILE_BLOCKED",
                decision_trace=blocked_context.to_dict()["decision_trace"],
                first_decisive_block=blocked_context.first_decisive_block,
            )

            for _ in range(2):
                store.save_decision_bundle(
                    config=config,
                    context=blocked_context,
                    audit=blocked_audit,
                    observation=blocked_observation,
                )

            counts = atomic_bundle_counts(db_path)
            self.assertEqual(counts["runtime_config_snapshots"], 1)
            self.assertEqual(counts["decision_contexts"], 1)
            self.assertEqual(counts["signal_audit"], 1)
            self.assertEqual(counts["observation_signals"], 1)
            self.assertEqual(counts["orders"], 0)

    def test_bundle_rejects_cross_references_without_partial_rows(self):
        cases = {
            "order symbol metadata": lambda fixture: {
                "order": replace(fixture[2], runtime_config_hash="0" * 64)
            },
            "audit decision": lambda fixture: {
                "audit": replace(
                    fixture[3],
                    signal=replace(fixture[3].signal, decision_id="other-decision"),
                )
            },
            "observation context": lambda fixture: {
                "observation": replace(fixture[5], context_version="OTHER")
            },
        }
        for label, mutate in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                db_path = Path(temp_dir) / "monitor.sqlite3"
                store = SQLiteMonitorStore(db_path)
                fixture = atomic_bundle_fixture()
                config, context, order, audit, entry_snapshot, observed = fixture
                arguments = {
                    "config": config,
                    "context": context,
                    "order": order,
                    "credit": None,
                    "entry_snapshot": entry_snapshot,
                    "audit": audit,
                    "observation": observed,
                }
                arguments.update(mutate(fixture))

                with self.assertRaises(ValueError):
                    store.save_open_order_decision(**arguments)

                self.assertEqual(
                    atomic_bundle_counts(db_path),
                    {key: 0 for key in atomic_bundle_counts(db_path)},
                )

    def test_each_open_bundle_step_failure_rolls_back_and_releases_lock(self):
        steps = (
            "config",
            "context",
            "order",
            "credit",
            "entry_snapshot",
            "audit",
            "observation",
        )
        for step in steps:
            with self.subTest(step=step), tempfile.TemporaryDirectory() as temp_dir:
                db_path = Path(temp_dir) / "monitor.sqlite3"
                store = SQLiteMonitorStore(db_path)
                config, context, order, audit, entry_snapshot, observed = (
                    atomic_bundle_fixture()
                )

                def fail_after(completed_step):
                    if completed_step == step:
                        raise RuntimeError(f"failed after {step}")

                with mock.patch.object(store, "_after_bundle_step", side_effect=fail_after):
                    with self.assertRaisesRegex(RuntimeError, f"failed after {step}"):
                        store.save_open_order_decision(
                            config=config,
                            context=context,
                            order=order,
                            credit=None,
                            entry_snapshot=entry_snapshot,
                            audit=audit,
                            observation=observed,
                        )

                self.assertEqual(
                    atomic_bundle_counts(db_path),
                    {key: 0 for key in atomic_bundle_counts(db_path)},
                )
                self.assertEqual(store.profile_summary_revision("BTCUSDT"), 0)
                store.save_decision_bundle(
                    config=config,
                    context=context,
                    audit=audit,
                )
                self.assertEqual(atomic_bundle_counts(db_path)["decision_contexts"], 1)

    def test_commit_failure_rolls_back_every_bundle_member_and_releases_lock(self):
        class FailCommitConnection(sqlite3.Connection):
            fail_commit = True

            def commit(self):
                if self.fail_commit:
                    self.fail_commit = False
                    raise sqlite3.OperationalError("injected commit failure")
                return super().commit()

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            config, context, _order, audit, _entry_snapshot, observed = (
                atomic_bundle_fixture()
            )
            real_connect = sqlite3.connect

            def fail_commit_connect(*args, **kwargs):
                return real_connect(*args, **kwargs, factory=FailCommitConnection)

            with mock.patch("app.storage.sqlite3.connect", side_effect=fail_commit_connect):
                with self.assertRaisesRegex(sqlite3.OperationalError, "commit failure"):
                    store.save_decision_bundle(
                        config=config,
                        context=context,
                        audit=audit,
                        observation=observed,
                    )

            self.assertEqual(
                atomic_bundle_counts(db_path),
                {key: 0 for key in atomic_bundle_counts(db_path)},
            )
            store.save_decision_bundle(config=config, context=context, audit=audit)

    def test_core_capacity_failure_writes_no_bundle_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            config, context, _order, audit, _entry_snapshot, observed = (
                atomic_bundle_fixture()
            )

            with mock.patch(
                "app.storage.capacity_from_connection",
                return_value=capacity_for_bytes(3 * 1024**3),
            ):
                with self.assertRaises(CoreStorageCapacityError):
                    store.save_decision_bundle(
                        config=config,
                        context=context,
                        audit=audit,
                        observation=observed,
                    )

            self.assertEqual(
                atomic_bundle_counts(db_path),
                {key: 0 for key in atomic_bundle_counts(db_path)},
            )

    def test_two_stores_concurrently_retry_same_bundle_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            first = SQLiteMonitorStore(db_path)
            second = SQLiteMonitorStore(db_path)
            config, context, order, audit, entry_snapshot, observed = (
                atomic_bundle_fixture()
            )
            barrier = threading.Barrier(2)
            errors = []

            def save(store):
                try:
                    barrier.wait(timeout=5)
                    store.save_open_order_decision(
                        config=config,
                        context=context,
                        order=order,
                        credit=None,
                        entry_snapshot=entry_snapshot,
                        audit=audit,
                        observation=observed,
                    )
                except Exception as error:  # noqa: BLE001 - test captures thread failures.
                    errors.append(error)

            threads = [
                threading.Thread(target=save, args=(store,))
                for store in (first, second)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertEqual(errors, [])
            counts = atomic_bundle_counts(db_path)
            self.assertEqual(counts["runtime_config_snapshots"], 1)
            self.assertEqual(counts["decision_contexts"], 1)
            self.assertEqual(counts["orders"], 1)
            self.assertEqual(counts["order_entry_snapshots"], 1)
            self.assertEqual(counts["signal_audit"], 1)
            self.assertEqual(counts["observation_signals"], 1)

    def test_settlement_updates_order_credit_and_snapshot_by_decision_id_atomically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            config, context, order, audit, entry_snapshot, _observed = (
                atomic_bundle_fixture(include_observation=False)
            )
            order = replace(order, stake_progression_version=TWO_STAGE_VERSION)
            store.save_open_order_decision(
                config=config,
                context=context,
                order=order,
                credit=None,
                entry_snapshot=entry_snapshot,
                audit=audit,
            )
            with closing(sqlite3.connect(db_path)) as connection:
                frozen_entry_payload = connection.execute(
                    "select entry_payload from order_entry_snapshots"
                ).fetchone()[0]
            settled = replace(
                order,
                status="SETTLED",
                result="WIN",
                settled_at=order.expires_at,
                exit_price=101.0,
                pnl=8.0,
            )
            credit = StakeProgressionCredit(
                source_order_id=order.id,
                created_at=order.expires_at,
                direction=order.direction,
            )

            store.save_settled_order_with_credit(settled, context.symbol, credit)

            with closing(sqlite3.connect(db_path)) as connection:
                connection.row_factory = sqlite3.Row
                order_row = connection.execute(
                    "select status, result, decision_id from orders"
                ).fetchone()
                entry_row = connection.execute(
                    "select decision_id, entry_payload, settlement_payload, result, pnl "
                    "from order_entry_snapshots"
                ).fetchone()
            self.assertEqual(tuple(order_row), ("SETTLED", "WIN", context.decision_id))
            self.assertEqual(entry_row["decision_id"], context.decision_id)
            self.assertEqual(entry_row["entry_payload"], frozen_entry_payload)
            self.assertEqual(entry_row["result"], "WIN")
            self.assertEqual(entry_row["pnl"], 8.0)
            self.assertEqual(
                json.loads(entry_row["settlement_payload"])["decision_id"],
                context.decision_id,
            )
            self.assertEqual(
                store.load_stake_progression_credits(context.symbol)[0].status,
                "PENDING",
            )

    def test_post_commit_profile_maintenance_failure_does_not_fail_settlement(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            config, context, order, audit, entry_snapshot, _observed = (
                atomic_bundle_fixture(include_observation=False)
            )
            store.save_open_order_decision(
                config=config,
                context=context,
                order=order,
                credit=None,
                entry_snapshot=entry_snapshot,
                audit=audit,
            )
            store.wait_for_profile_summary_rebuilds(timeout=10)
            settled = replace(
                order,
                status="SETTLED",
                result="WIN",
                settled_at=order.expires_at,
                exit_price=101.0,
                pnl=8.0,
            )

            with mock.patch.object(
                store,
                "_refresh_profile_summary_cache",
                side_effect=RuntimeError("cache maintenance failed"),
            ):
                store.save_settled_order_with_credit(settled, "BTCUSDT", None)

            restored = store.load_orders("BTCUSDT")
            snapshots = store.load_order_entry_snapshots("BTCUSDT")
            self.assertEqual((restored[0].status, restored[0].result), ("SETTLED", "WIN"))
            self.assertEqual(snapshots[0]["result"], "WIN")
            self.assertIn("BTCUSDT", store._profile_summary_dirty)

    def test_settlement_snapshot_failure_rolls_back_order_and_credit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            config, context, order, audit, entry_snapshot, _observed = (
                atomic_bundle_fixture(include_observation=False)
            )
            order = replace(order, stake_progression_version=TWO_STAGE_VERSION)
            store.save_open_order_decision(
                config=config,
                context=context,
                order=order,
                credit=None,
                entry_snapshot=entry_snapshot,
                audit=audit,
            )
            settled = replace(
                order,
                status="SETTLED",
                result="WIN",
                settled_at=order.expires_at,
                exit_price=101.0,
                pnl=8.0,
            )
            credit = StakeProgressionCredit(
                source_order_id=order.id,
                created_at=order.expires_at,
                direction=order.direction,
            )

            with mock.patch.object(
                store,
                "_update_order_entry_snapshot_settlement",
                side_effect=RuntimeError("snapshot settlement failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "snapshot settlement failed"):
                    store.save_settled_order_with_credit(
                        settled,
                        context.symbol,
                        credit,
                    )

            restored = store.load_orders(context.symbol)[0]
            self.assertEqual(restored.status, "OPEN")
            self.assertEqual(store.load_stake_progression_credits(context.symbol), [])

    def test_settlement_rejects_changes_to_frozen_order_identity(self):
        mutations = {
            "entry_price": lambda order: replace(order, entry_price=order.entry_price + 1),
            "opened_at": lambda order: replace(order, opened_at=order.opened_at + 1),
            "decision_id": lambda order: replace(order, decision_id="different-decision"),
            "direction": lambda order: replace(order, direction="SHORT"),
            "timeframe": lambda order: replace(order, timeframe_minutes=1),
            "strategy": lambda order: replace(order, strategy_family="different"),
            "profile": lambda order: replace(order, profile_key="different-profile"),
            "stake": lambda order: replace(order, stake=order.stake + 1),
            "expires_at": lambda order: replace(order, expires_at=order.expires_at + 1),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                db_path = Path(temp_dir) / "monitor.sqlite3"
                store = SQLiteMonitorStore(db_path)
                config, context, order, audit, entry_snapshot, _observed = (
                    atomic_bundle_fixture(include_observation=False)
                )
                store.save_open_order_decision(
                    config=config,
                    context=context,
                    order=order,
                    credit=None,
                    entry_snapshot=entry_snapshot,
                    audit=audit,
                )
                settled = replace(
                    order,
                    status="SETTLED",
                    result="WIN",
                    settled_at=order.expires_at,
                    exit_price=101.0,
                    pnl=8.0,
                )

                with self.assertRaisesRegex(ValueError, "frozen order"):
                    store.save_settled_order_with_credit(
                        mutate(settled),
                        context.symbol,
                        None,
                    )

                self.assertEqual(store.load_orders(context.symbol)[0], order)

    def test_adaptive_loader_is_exact_causal_and_not_limited_to_five_hundred(self):
        evaluated_at = 1_800_000_000_000
        profile_key = "10|short_extension|normal_down_short_extension_observe|SHORT|WD-02"
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            rows = [
                observation(
                    f"adaptive-{index}",
                    opened_at=evaluated_at - 8 * 86_400_000 + index * 660_000,
                )
                for index in range(520)
            ]
            other = observation(
                "adaptive-other-profile",
                tag="other",
                opened_at=evaluated_at - 60_000,
            )
            future = replace(
                observation(
                    "adaptive-cutoff-equal",
                    opened_at=evaluated_at - 600_000,
                ),
                expires_at=evaluated_at,
                settled_at=evaluated_at,
            )
            old = observation(
                "adaptive-too-old",
                opened_at=evaluated_at - 16 * 86_400_000,
            )
            store.save_observations([*rows, other, future, old], "BTCUSDT")

            restored = store.load_adaptive_profile_observations(
                "BTCUSDT",
                lookback_days=15,
                evaluated_at=evaluated_at,
                profile_keys={profile_key},
            )
            store.close()

        self.assertEqual(len(restored), 520)
        self.assertTrue(all(item.settled_at < evaluated_at for item in restored))
        self.assertEqual(
            {item.strategy_tag for item in restored},
            {"normal_down_short_extension_observe"},
        )

    def test_adaptive_loader_requires_fifteen_day_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            with self.assertRaisesRegex(ValueError, "at least 15 days"):
                store.load_adaptive_profile_observations(
                    "BTCUSDT",
                    lookback_days=14,
                    evaluated_at=1_800_000_000_000,
                )
            store.close()

    def test_adaptive_loader_legacy_profile_key_matches_complete_structured_key(self):
        evaluated_at = 1_800_000_000_000
        complete_key = (
            "10|short_extension|normal_down_short_extension_observe|SHORT|WD-02"
        )
        legacy = replace(
            observation(
                "adaptive-legacy-profile-key",
                opened_at=evaluated_at - 11 * 60_000,
            ),
            profile_key="short_extension|SHORT|WD-02",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_observation(legacy, "BTCUSDT")

            full = store.load_adaptive_profile_observations(
                "BTCUSDT",
                lookback_days=15,
                evaluated_at=evaluated_at,
            )
            exact = store.load_adaptive_profile_observations(
                "BTCUSDT",
                lookback_days=15,
                evaluated_at=evaluated_at,
                profile_keys={complete_key},
            )
            store.close()

        self.assertEqual([item.observation_key for item in full], [legacy.observation_key])
        self.assertEqual([item.observation_key for item in exact], [legacy.observation_key])

    def test_observation_profile_paths_share_complete_structured_identity(self):
        evaluated_at = 1_800_000_000_000
        complete_key = (
            "10|short_extension|normal_down_short_extension_observe|SHORT|WD-02"
        )
        rows = [
            replace(
                observation(
                    "adaptive-empty-profile-key",
                    opened_at=evaluated_at - 12 * 60_000,
                ),
                profile_key="",
            ),
            replace(
                observation(
                    "adaptive-three-part-profile-key",
                    opened_at=evaluated_at - 11 * 60_000,
                ),
                profile_key="short_extension|SHORT|WD-02",
            ),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMonitorStore(Path(temp_dir) / "monitor.sqlite3")
            store.save_observations(rows, "BTCUSDT")

            adaptive = store.load_adaptive_profile_observations(
                "BTCUSDT",
                lookback_days=15,
                evaluated_at=evaluated_at,
                profile_keys={complete_key},
            )
            page = store.page_observations(
                "BTCUSDT",
                profile=complete_key,
            )
            store.close()

        expected_keys = {row.observation_key for row in rows}
        self.assertEqual(
            {item.observation_key for item in adaptive},
            expected_keys,
        )
        self.assertEqual(
            {item["observation_key"] for item in page["observations"]},
            expected_keys,
        )
        self.assertEqual(page["total"], 2)
        self.assertIn(complete_key.upper(), page["filter_options"]["profile"])
        self.assertNotIn(
            "SHORT_EXTENSION|SHORT|WD-02",
            page["filter_options"]["profile"],
        )

    def test_adaptive_loader_uses_settled_at_range_index(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "monitor.sqlite3"
            store = SQLiteMonitorStore(db_path)
            store.close()
            with closing(sqlite3.connect(db_path)) as connection:
                indexes = {
                    row[1]
                    for row in connection.execute(
                        "pragma index_list(observation_signals)"
                    )
                }
                plan = connection.execute(
                    """
                    explain query plan
                    select observation_signals.payload
                    from observation_signals
                    left join decision_contexts
                      on decision_contexts.symbol = observation_signals.symbol
                     and decision_contexts.decision_id = observation_signals.decision_id
                    where observation_signals.symbol = ?
                      and observation_signals.status = 'SETTLED'
                      and observation_signals.settled_at >= ?
                      and observation_signals.settled_at < ?
                    order by observation_signals.settled_at,
                             observation_signals.opened_at,
                             observation_signals.observation_key
                    """,
                    ("BTCUSDT", 1_000, 2_000),
                ).fetchall()

        index_name = "idx_observation_signals_symbol_status_settled"
        self.assertIn(index_name, indexes)
        detail = " ".join(str(row[3]) for row in plan)
        self.assertIn(index_name, detail)
        self.assertIn("settled_at>?", detail)
        self.assertIn("settled_at<?", detail)


if __name__ == "__main__":
    unittest.main()
