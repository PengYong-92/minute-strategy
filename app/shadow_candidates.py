from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, fields

from app.profile_admission import ProfileAdmissionPolicy, policy_grid
from app.shadow_models import ShadowParameterSnapshot


PROFILE_ADMISSION_SHADOW_VERSION = "PROFILE_ADMISSION_SHADOW_V1"


@dataclass(frozen=True)
class ShadowArmDefinition:
    role: str
    policy: ProfileAdmissionPolicy
    parameters: ShadowParameterSnapshot
    analyzer_hash: str

    @property
    def parameter_hash(self) -> str:
        return self.parameters.parameter_hash

    @property
    def complexity(self) -> int:
        return self.policy.complexity


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _stable_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("ascii")).hexdigest()


def _policy_from_dict(payload: dict[str, object]) -> ProfileAdmissionPolicy:
    return ProfileAdmissionPolicy(**deepcopy(payload))


def _admission_status(policy: ProfileAdmissionPolicy) -> dict[str, object]:
    return {
        "enabled": policy.fast_enabled,
        "policy": policy.to_dict(),
        "policy_hash": policy.policy_hash,
        "policy_version": policy.version,
        "stability_proven": False,
        "release_allowed": False,
        "release_status": "SHADOW",
        "release_reason": "等待完整前向样本门槛",
    }


def _runtime_for_policy(
    runtime: dict[str, object],
    policy: ProfileAdmissionPolicy,
) -> dict[str, object]:
    result = deepcopy(runtime)
    profiles = result.setdefault("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("runtime profiles must be a mapping")
    profiles["admission"] = _admission_status(policy)
    return result


def _analyzer_payload(runtime: dict[str, object]) -> dict[str, object]:
    result = deepcopy(runtime)
    profiles = result.get("profiles")
    if isinstance(profiles, dict):
        profiles.pop("admission", None)
    return result


def _value_distance(left: object, right: object) -> float:
    if left == right:
        return 0.0
    if left is None or right is None:
        return 1.0
    if type(left) is bool and type(right) is bool:
        return 1.0
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return min(10.0, abs(float(left) - float(right)))
    if isinstance(left, (tuple, list)) and isinstance(right, (tuple, list)):
        return float(len(set(left).symmetric_difference(set(right))) or 1)
    return 1.0


def _policy_distance(
    champion: ProfileAdmissionPolicy,
    challenger: ProfileAdmissionPolicy,
) -> tuple[float, int, str]:
    distance = sum(
        _value_distance(getattr(champion, item.name), getattr(challenger, item.name))
        for item in fields(ProfileAdmissionPolicy)
        if item.init
    )
    return distance, challenger.complexity, challenger.policy_hash


def build_profile_admission_arms(
    seed: dict[str, object],
    *,
    max_challengers: int = 7,
) -> tuple[ShadowArmDefinition, ...]:
    limit = min(7, max(0, int(max_challengers)))
    champion_payload = seed.get("profile_admission_policy")
    runtime_snapshot = seed.get("runtime_config")
    if not isinstance(champion_payload, dict):
        raise ValueError("seed requires profile_admission_policy")
    if not isinstance(runtime_snapshot, dict):
        raise ValueError("seed requires runtime_config")
    canonical_payload = runtime_snapshot.get("canonical_payload")
    if not isinstance(canonical_payload, str):
        raise ValueError("runtime_config requires canonical_payload")
    runtime = json.loads(canonical_payload)
    if not isinstance(runtime, dict):
        raise ValueError("runtime_config canonical payload must be a mapping")

    champion = _policy_from_dict(champion_payload)
    analyzer_hash = _stable_hash(_analyzer_payload(runtime))
    candidates = sorted(
        (
            policy
            for policy in policy_grid()
            if policy.policy_hash != champion.policy_hash
        ),
        key=lambda policy: _policy_distance(champion, policy),
    )[:limit]

    definitions = []
    for role, policy in (
        [("CHAMPION", champion)]
        + [("CHALLENGER", item) for item in candidates]
    ):
        full_runtime = _runtime_for_policy(runtime, policy)
        parameters = ShadowParameterSnapshot(
            family="PROFILE_ADMISSION",
            version=PROFILE_ADMISSION_SHADOW_VERSION,
            parameters={
                "analyzer_hash": analyzer_hash,
                "base_runtime_hash": str(runtime_snapshot.get("hash", "")),
                "runtime_config": full_runtime,
                "profile_admission_policy": policy.to_dict(),
            },
        )
        definitions.append(
            ShadowArmDefinition(
                role=role,
                policy=policy,
                parameters=parameters,
                analyzer_hash=analyzer_hash,
            )
        )
    return tuple(definitions)
