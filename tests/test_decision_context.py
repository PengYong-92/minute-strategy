import inspect
import json
import math
import unittest
from dataclasses import FrozenInstanceError, dataclass

from app.decision_context import (
    CONTEXT_VERSION,
    DecisionContext,
    DecisionContextBuilder,
    runtime_config_snapshot,
)


@dataclass
class NestedConfig:
    windows: tuple[int, ...]
    labels: set[str]


class DecisionContextTest(unittest.TestCase):
    def new_builder(self, **overrides):
        arguments = {
            "symbol": "btcusdt",
            "closed_kline_at_ms": 1_700_000_000_000,
            "candidate_origin": "strategy",
            "runtime_config_hash": "config-hash",
        }
        arguments.update(overrides)
        return DecisionContextBuilder.new(**arguments)

    def finish(self, builder):
        return builder.finish(
            final_decision="OPEN",
            final_reason="accepted",
            open_allowed=True,
            observation_allowed=True,
        )

    def trace_record(self, **overrides):
        record = {
            "stage": "risk",
            "result": "PASS",
            "decisive_values": None,
            "reason_code": "",
        }
        record.update(overrides)
        return record

    def direct_context(self, **overrides):
        arguments = {
            "decision_id": "decision-id",
            "context_version": CONTEXT_VERSION,
            "runtime_config_hash": "config-hash",
            "strategy_build_id": "build-7",
            "symbol": "BTCUSDT",
            "closed_kline_at_ms": 1_700_000_000_000,
            "candidate_origin": "strategy",
            "inputs": {},
            "decision_trace": (),
            "first_decisive_block": "",
            "final_decision": "OPEN",
            "final_reason": "accepted",
            "open_allowed": True,
            "observation_allowed": True,
        }
        arguments.update(overrides)
        return DecisionContext(**arguments)

    def test_builder_public_methods_have_explicit_annotations(self):
        for method in (
            DecisionContextBuilder.new,
            DecisionContextBuilder.capture_inputs,
            DecisionContextBuilder.trace,
            DecisionContextBuilder.finish,
        ):
            with self.subTest(method=method.__name__):
                signature = inspect.signature(method)
                for name, parameter in signature.parameters.items():
                    if name not in {"self", "cls"}:
                        self.assertIsNot(parameter.annotation, inspect.Parameter.empty)
                self.assertIsNot(signature.return_annotation, inspect.Signature.empty)

    def test_config_key_order_does_not_affect_hash(self):
        first = runtime_config_snapshot(
            {"threshold": 1.5, "guard": {"enabled": True, "window": 20}},
            strategy_build_id="build-a",
        )
        second = runtime_config_snapshot(
            {"guard": {"window": 20, "enabled": True}, "threshold": 1.5},
            strategy_build_id="build-b",
        )

        self.assertEqual(first.hash, second.hash)
        self.assertEqual(first.canonical_payload, second.canonical_payload)
        self.assertNotEqual(first.strategy_build_id, second.strategy_build_id)

    def test_golden_config_hash_and_decision_id(self):
        snapshot = runtime_config_snapshot(
            {
                "threshold": 1.5,
                "guard": {"enabled": True, "window": 20},
                "labels": {"beta", "alpha"},
                "api_key": "excluded",
            },
            strategy_build_id="build-7",
        )
        builder = DecisionContextBuilder.new(
            "btcusdt",
            1_700_000_000_000,
            "strategy",
            snapshot.hash,
            strategy_build_id="build-7",
            profile_key="profile-a",
            candidate_ordinal=2,
        )
        builder.capture_inputs({})
        context = self.finish(builder)

        self.assertEqual(
            snapshot.canonical_payload,
            '{"guard":{"enabled":true,"window":20},'
            '"labels":["alpha","beta"],"threshold":1.5}',
        )
        self.assertEqual(
            snapshot.hash,
            "e076670441aeacf49d3a21c390ec5a5613ced9f71ec4bc12442177c32b7cda8c",
        )
        self.assertEqual(
            context.decision_id,
            "7a792c6dec68833d181fbf892a255cc6e1afd60ce1fb5bf9f145282d256e6b9d",
        )

    def test_recursive_credentials_are_excluded_and_do_not_affect_hash(self):
        first = runtime_config_snapshot(
            {
                "api_key": "first-key",
                "notifications": {
                    "webhook_url": "https://first.invalid",
                    "items": [
                        {"webhook_token": "first-token", "enabled": True},
                        {"api_secret": "first-secret", "name": "primary"},
                    ],
                },
            }
        )
        second = runtime_config_snapshot(
            {
                "api_key": "second-key",
                "notifications": {
                    "webhook_url": "https://second.invalid",
                    "items": [
                        {"webhook_token": "second-token", "enabled": True},
                        {"api_secret": "second-secret", "name": "primary"},
                    ],
                },
            }
        )

        self.assertEqual(first.hash, second.hash)
        self.assertEqual(
            json.loads(first.canonical_payload),
            {"notifications": {"items": [{"enabled": True}, {"name": "primary"}]}},
        )
        for credential_key in ("webhook_url", "webhook_token", "api_key", "api_secret"):
            self.assertNotIn(credential_key, first.canonical_payload)

    def test_set_list_and_dataclass_normalization_is_deterministic(self):
        first = runtime_config_snapshot(
            {
                "nested": NestedConfig(windows=(30, 10), labels={"beta", "alpha"}),
                "levels": [3, 1, 2],
            }
        )
        second = runtime_config_snapshot(
            {
                "levels": [3, 1, 2],
                "nested": NestedConfig(windows=(30, 10), labels={"alpha", "beta"}),
            }
        )

        self.assertEqual(first.hash, second.hash)
        self.assertEqual(
            json.loads(first.canonical_payload),
            {
                "levels": [3, 1, 2],
                "nested": {"labels": ["alpha", "beta"], "windows": [30, 10]},
            },
        )

    def test_nan_and_unsupported_values_fail(self):
        with self.assertRaises(ValueError):
            runtime_config_snapshot({"threshold": math.nan})
        with self.assertRaises(TypeError):
            runtime_config_snapshot({"value": object()})

    def test_builder_freezes_caller_inputs_and_records_ordered_traces(self):
        inputs = {"features": {"score": 7}, "windows": [10, 30]}
        decisive_values = {"minimum": 5, "observed": [7]}
        builder = self.new_builder()
        builder.capture_inputs(inputs)
        builder.trace("quality", "PASS", decisive_values, reason_code="QUALITY_OK")
        builder.trace("session", "ALLOW")

        inputs["features"]["score"] = -1
        inputs["windows"].append(60)
        decisive_values["observed"].append(-1)
        context = self.finish(builder)

        self.assertEqual(context.context_version, CONTEXT_VERSION)
        self.assertEqual(context.symbol, "BTCUSDT")
        self.assertEqual(context.inputs["features"]["score"], 7)
        self.assertEqual(context.inputs["windows"], (10, 30))
        self.assertEqual(
            [record["stage"] for record in context.decision_trace],
            ["quality", "session"],
        )
        self.assertEqual(context.decision_trace[0]["decisive_values"]["observed"], (7,))
        self.assertEqual(context.first_decisive_block, "")
        with self.assertRaises(TypeError):
            context.inputs["features"]["score"] = 0
        with self.assertRaises(FrozenInstanceError):
            context.final_decision = "BLOCK"

    def test_first_decisive_block_is_preserved(self):
        builder = self.new_builder()
        builder.capture_inputs({"score": 9})
        builder.trace("quality", "PASS")
        builder.trace("risk", "BLOCK", {"risk": "high"}, "RISK_HIGH")
        builder.trace("capacity", "BLOCK", {"open_orders": 2}, "AT_CAPACITY")

        context = builder.finish("BLOCK", "risk high", False, True)

        self.assertEqual(context.first_decisive_block, "risk")
        self.assertEqual(context.decision_trace[1]["reason_code"], "RISK_HIGH")
        self.assertEqual(
            context.decision_trace[1]["decisive_values"],
            {"risk": "high"},
        )

    def test_invalid_trace_metadata_is_rejected_before_first_block(self):
        builder = self.new_builder()
        builder.capture_inputs({"score": 9})

        invalid_calls = (
            ((object(), "BLOCK"), TypeError),
            (("risk", object()), TypeError),
            (("risk", "BLOCK", None, object()), TypeError),
            (("", "BLOCK"), ValueError),
            (("risk", ""), ValueError),
        )
        for arguments, error in invalid_calls:
            with self.subTest(arguments=arguments):
                with self.assertRaises(error):
                    builder.trace(*arguments)

        builder.trace("risk", "BLOCK", {"risk": "high"}, "RISK_HIGH")
        builder.trace("capacity", "BLOCK", {"open_orders": 2}, "AT_CAPACITY")
        context = builder.finish("BLOCK", "risk high", False, True)

        self.assertEqual(context.first_decisive_block, "risk")
        self.assertEqual(len(context.decision_trace), 2)

    def test_invalid_finish_outcomes_are_rejected_without_finishing_builder(self):
        builder = self.new_builder()
        builder.capture_inputs({})
        invalid_calls = (
            ((object(), "reason", True, True), TypeError),
            (("", "reason", True, True), ValueError),
            (("OPEN", object(), True, True), TypeError),
            (("OPEN", "reason", 1, True), TypeError),
            (("OPEN", "reason", True, 0), TypeError),
        )
        for arguments, error in invalid_calls:
            with self.subTest(arguments=arguments):
                with self.assertRaises(error):
                    builder.finish(*arguments)

        context = builder.finish("BLOCK", "", False, True)
        self.assertEqual(context.final_reason, "")

    def test_direct_construction_deep_freezes_and_copies_nested_values(self):
        inputs = {"features": {"scores": [7, 8]}}
        decisive_values = {"flags": ["spread"]}
        trace_record = {
            "stage": "risk",
            "result": "BLOCK",
            "decisive_values": decisive_values,
            "reason_code": "RISK_HIGH",
        }
        context = self.direct_context(
            inputs=inputs,
            decision_trace=(trace_record,),
            first_decisive_block="risk",
            final_decision="BLOCK",
            open_allowed=False,
        )

        inputs["features"]["scores"].append(-1)
        decisive_values["flags"].append("volume")
        trace_record["reason_code"] = "MUTATED"

        self.assertEqual(context.inputs["features"]["scores"], (7, 8))
        self.assertEqual(
            context.decision_trace[0]["decisive_values"]["flags"],
            ("spread",),
        )
        self.assertEqual(context.decision_trace[0]["reason_code"], "RISK_HIGH")
        with self.assertRaises(TypeError):
            context.decision_trace[0]["reason_code"] = "CHANGED"
        persisted = context.to_dict()
        json.dumps(persisted, allow_nan=False)
        persisted["inputs"]["features"]["scores"].append(9)
        self.assertEqual(context.inputs["features"]["scores"], (7, 8))

    def test_direct_construction_rejects_invalid_trace_and_outcome_metadata(self):
        invalid_contexts = (
            (
                {
                    "decision_trace": (
                        self.trace_record(stage=object()),
                    ),
                },
                TypeError,
            ),
            (
                {"decision_trace": (self.trace_record(result=""),)},
                ValueError,
            ),
            (
                {
                    "decision_trace": (
                        self.trace_record(reason_code=object()),
                    ),
                },
                TypeError,
            ),
            (
                {
                    "decision_trace": (
                        self.trace_record(decisive_values=object()),
                    ),
                },
                TypeError,
            ),
            ({"first_decisive_block": object()}, TypeError),
            ({"final_decision": object()}, TypeError),
            ({"final_decision": ""}, ValueError),
            ({"final_reason": object()}, TypeError),
            ({"open_allowed": 1}, TypeError),
            ({"observation_allowed": 0}, TypeError),
        )
        for overrides, error in invalid_contexts:
            with self.subTest(overrides=overrides):
                with self.assertRaises(error):
                    self.direct_context(**overrides)

    def test_builder_lifecycle_rejects_invalid_calls(self):
        builder = self.new_builder()
        with self.assertRaises(RuntimeError):
            builder.trace("quality", "PASS")
        with self.assertRaises(RuntimeError):
            self.finish(builder)

        builder.capture_inputs({"score": 1})
        with self.assertRaises(RuntimeError):
            builder.capture_inputs({"score": 2})

        self.finish(builder)
        with self.assertRaises(RuntimeError):
            self.finish(builder)

    def test_decision_id_is_stable_and_distinguishes_candidate_identity(self):
        common = {
            "symbol": "btcusdt",
            "closed_kline_at_ms": 1_700_000_000_000,
            "candidate_origin": "strategy",
            "runtime_config_hash": "config-hash",
            "strategy_build_id": "build-7",
            "profile_key": "profile-a",
            "candidate_ordinal": 0,
        }

        first = DecisionContextBuilder.new(**common)
        same = DecisionContextBuilder.new(**{**common, "symbol": "BTCUSDT"})
        different_profile = DecisionContextBuilder.new(**{**common, "profile_key": "profile-b"})
        different_ordinal = DecisionContextBuilder.new(**{**common, "candidate_ordinal": 1})
        for builder in (first, same, different_profile, different_ordinal):
            builder.capture_inputs({})

        first_context = self.finish(first)
        same_context = self.finish(same)
        profile_context = self.finish(different_profile)
        ordinal_context = self.finish(different_ordinal)

        self.assertEqual(first_context.decision_id, same_context.decision_id)
        self.assertEqual(len(first_context.decision_id), 64)
        self.assertNotEqual(first_context.decision_id, profile_context.decision_id)
        self.assertNotEqual(first_context.decision_id, ordinal_context.decision_id)

    def test_to_dict_returns_json_serializable_copies(self):
        snapshot = runtime_config_snapshot({"labels": {"beta", "alpha"}})
        builder = self.new_builder(strategy_build_id="build-7")
        builder.capture_inputs({"features": {"score": 7}})
        builder.trace("risk", "BLOCK", {"flags": ["spread"]}, "RISK")
        context = builder.finish("BLOCK", "risk", False, True)

        snapshot_dict = snapshot.to_dict()
        context_dict = context.to_dict()
        json.dumps(snapshot_dict, allow_nan=False)
        json.dumps(context_dict, allow_nan=False)

        self.assertEqual(context_dict["first_decisive_block"], "risk")
        context_dict["inputs"]["features"]["score"] = -1
        context_dict["decision_trace"][0]["decisive_values"]["flags"].append("volume")
        self.assertEqual(context.inputs["features"]["score"], 7)
        self.assertEqual(context.decision_trace[0]["decisive_values"]["flags"], ("spread",))
        self.assertEqual(snapshot_dict["hash"], snapshot.hash)


if __name__ == "__main__":
    unittest.main()
