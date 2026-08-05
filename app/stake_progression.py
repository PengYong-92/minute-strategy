from dataclasses import asdict, dataclass
import math
from typing import Iterable


TWO_STAGE_VERSION = "TWO_STAGE_V1"
_CREDIT_STATUSES = {"PENDING", "CONSUMED", "CANCELLED"}
_RESULTS = {"WIN", "LOSS"}


@dataclass
class StakeProgressionCredit:
    source_order_id: int
    created_at: int
    credit_id: str = ""
    consumed_at: int | None = None
    consumed_order_id: int | None = None
    version: str = TWO_STAGE_VERSION
    status: str = "PENDING"

    def __post_init__(self) -> None:
        self.source_order_id = int(self.source_order_id)
        if self.source_order_id <= 0:
            raise ValueError("source_order_id must be > 0")
        self.created_at = int(self.created_at)
        if self.created_at < 0:
            raise ValueError("created_at must be >= 0")
        self.consumed_at = None if self.consumed_at is None else int(self.consumed_at)
        if self.consumed_at is not None and self.consumed_at < 0:
            raise ValueError("consumed_at must be >= 0")
        self.consumed_order_id = (
            None if self.consumed_order_id is None else int(self.consumed_order_id)
        )
        self.version = str(self.version).strip()
        if not self.version:
            raise ValueError("version must not be empty")
        if not self.credit_id:
            self.credit_id = f"{self.version}:{self.source_order_id}"
        else:
            self.credit_id = str(self.credit_id)
        self.status = str(self.status).upper()
        if self.status not in _CREDIT_STATUSES:
            raise ValueError(f"unknown credit status: {self.status}")
        if self.status in {"PENDING", "CANCELLED"}:
            if self.consumed_order_id is not None or self.consumed_at is not None:
                raise ValueError(
                    f"{self.status} credit must not contain consumption fields"
                )
        else:
            if self.consumed_order_id is None or self.consumed_at is None:
                raise ValueError(
                    "CONSUMED credit requires consumed_order_id and consumed_at"
                )
            if self.consumed_order_id <= 0:
                raise ValueError("consumed_order_id must be > 0")
            if self.consumed_at < self.created_at:
                raise ValueError("consumed_at must be >= created_at")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class OrderTerms:
    stake: float
    win_return: float
    step: int
    source_order_id: int | None = None
    version: str = TWO_STAGE_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


class TwoStageStakeProgression:
    def __init__(
        self,
        *,
        enabled: bool,
        base_stake: float = 10.0,
        base_win_return: float | None = None,
        max_active: int | None = None,
        max_open_orders: int = 5,
        activated_at: int | None = None,
        credits: Iterable[StakeProgressionCredit] = (),
        active_second_orders: int | None = None,
        active_second_order_ids: Iterable[int] | None = None,
        second_stake: float | None = None,
        payout_ratio: float | None = None,
        max_active_second_stage: int | None = None,
        activation_time: int | None = None,
    ) -> None:
        self._enabled = bool(enabled)
        self.base_stake = self._positive_float(base_stake, "base_stake")
        self.base_win_return, self.payout_ratio = self._resolve_base_return(
            base_win_return,
            payout_ratio,
        )
        resolved_second_stake = self.base_win_return if second_stake is None else second_stake
        self.second_stake = self._positive_float(resolved_second_stake, "second_stake")
        second_win_return = self.second_stake * (1.0 + self.payout_ratio)
        if not math.isfinite(second_win_return):
            raise ValueError("second_win_return must be finite")
        self.second_win_return = round(second_win_return, 4)

        self.max_open_orders = self._positive_int(max_open_orders, "max_open_orders")
        requested_max_active = self._resolve_alias(
            max_active,
            max_active_second_stage,
            default=1,
            primary_name="max_active",
            alias_name="max_active_second_stage",
        )
        requested_max_active = self._positive_int(requested_max_active, "max_active")
        self.max_active = min(requested_max_active, self.max_open_orders)
        self.max_active_second_stage = self.max_active

        resolved_activation = self._resolve_alias(
            activated_at,
            activation_time,
            default=0,
            primary_name="activated_at",
            alias_name="activation_time",
        )
        self.activated_at = int(resolved_activation)
        self.activation_time = self.activated_at
        self.credits = list(credits)
        if not all(isinstance(item, StakeProgressionCredit) for item in self.credits):
            raise TypeError("credits must contain StakeProgressionCredit instances")
        self._validate_restored_credits()

        self._active_second_order_ids: set[int] = set()
        self._processed_first_stage_order_ids = {
            credit.source_order_id
            for credit in self.credits
            if credit.version == TWO_STAGE_VERSION
        }
        self.reconcile(
            active_second_orders=active_second_orders,
            active_second_order_ids=active_second_order_ids,
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def active_second_orders(self) -> int:
        return len(self._active_second_order_ids)

    @property
    def active_second_order_ids(self) -> frozenset[int]:
        return frozenset(self._active_second_order_ids)

    def assign(
        self,
        order_id: int,
        opened_at: int,
    ) -> tuple[OrderTerms, StakeProgressionCredit | None]:
        normalized_order_id = self._positive_order_id(order_id)
        normalized_opened_at = self._non_negative_timestamp(opened_at, "opened_at")
        existing = next(
            (
                credit
                for credit in self.credits
                if credit.status == "CONSUMED"
                and credit.consumed_order_id == normalized_order_id
            ),
            None,
        )
        if existing is not None:
            return self._second_terms(existing.source_order_id), existing

        if not self.enabled:
            return self._base_terms(), None

        pending = sorted(
            (
                credit
                for credit in self.credits
                if credit.status == "PENDING"
                and credit.created_at <= normalized_opened_at
            ),
            key=lambda credit: (credit.created_at, credit.credit_id),
        )
        if not pending:
            return self._base_terms(), None

        credit = pending[0]
        credit.consumed_order_id = normalized_order_id
        credit.consumed_at = normalized_opened_at
        credit.status = "CONSUMED"
        self._active_second_order_ids.add(normalized_order_id)
        return self._second_terms(credit.source_order_id), credit

    def settle(
        self,
        order_id: int,
        order_opened_at: int,
        step: int,
        result: str,
        settled_at: int,
    ) -> StakeProgressionCredit | None:
        normalized_step = self._validate_step(step)
        normalized_result = self._validate_result(result)
        normalized_order_id = self._positive_order_id(order_id)
        normalized_opened_at = self._non_negative_timestamp(
            order_opened_at,
            "order_opened_at",
        )
        normalized_settled_at = self._non_negative_timestamp(
            settled_at,
            "settled_at",
        )
        if normalized_settled_at < normalized_opened_at:
            raise ValueError("settled_at must be >= order_opened_at")

        if normalized_step == 2:
            self._active_second_order_ids.discard(normalized_order_id)
            return None

        existing = next(
            (
                credit
                for credit in self.credits
                if credit.source_order_id == normalized_order_id
                and credit.version == TWO_STAGE_VERSION
            ),
            None,
        )
        if existing is not None:
            return existing
        if normalized_order_id in self._processed_first_stage_order_ids:
            return None

        if (
            not self.enabled
            or normalized_opened_at < self.activated_at
            or normalized_result != "WIN"
        ):
            self._processed_first_stage_order_ids.add(normalized_order_id)
            return None

        credit = StakeProgressionCredit(
            source_order_id=normalized_order_id,
            created_at=normalized_settled_at,
            status=(
                "CANCELLED"
                if self._reserved_count() >= self.max_active
                else "PENDING"
            ),
        )
        self.credits.append(credit)
        self._processed_first_stage_order_ids.add(normalized_order_id)
        return credit

    def set_enabled(self, enabled: bool) -> list[StakeProgressionCredit]:
        self._enabled = bool(enabled)
        if self._enabled:
            return []
        return self.cancel_pending()

    def cancel_pending(self) -> list[StakeProgressionCredit]:
        cancelled = []
        for credit in self.credits:
            if credit.status != "PENDING":
                continue
            credit.status = "CANCELLED"
            cancelled.append(credit)
        return cancelled

    def reconcile(
        self,
        *,
        active_second_orders: int | None = None,
        active_second_order_ids: Iterable[int] | None = None,
    ) -> list[StakeProgressionCredit]:
        restored_ids = self._resolve_active_second_order_ids(
            active_second_orders,
            active_second_order_ids,
        )
        if restored_ids is not None:
            self._active_second_order_ids = restored_ids

        if not self.enabled:
            return self.cancel_pending()

        pending = sorted(
            (credit for credit in self.credits if credit.status == "PENDING"),
            key=lambda credit: (credit.created_at, credit.credit_id),
            reverse=True,
        )
        excess = max(
            0,
            len(pending) + self.active_second_orders - self.max_active,
        )
        cancelled = pending[:excess]
        for credit in cancelled:
            credit.status = "CANCELLED"
        return cancelled

    def status(self) -> dict:
        pending = sum(credit.status == "PENDING" for credit in self.credits)
        next_terms = (
            self._second_terms()
            if self.enabled and pending > 0
            else self._base_terms()
        )
        return {
            "enabled": self.enabled,
            "version": TWO_STAGE_VERSION,
            "max_orders": 2,
            "pending_credits": pending,
            "active_second_stage": self.active_second_orders,
            "active_second_orders": self.active_second_orders,
            "active_second_order_ids": sorted(
                order_id
                for order_id in self._active_second_order_ids
                if order_id > 0
            ),
            "max_active_second_stage": self.max_active,
            "max_active": self.max_active,
            "base_stake": round(self.base_stake, 4),
            "base_win_return": round(self.base_win_return, 4),
            "second_stake": round(self.second_stake, 4),
            "second_win_return": round(self.second_win_return, 4),
            "payout_ratio": round(self.payout_ratio, 8),
            "next_stake": next_terms.stake,
            "next_win_return": next_terms.win_return,
            "next_step": next_terms.step,
        }

    def _reserved_count(self) -> int:
        pending = sum(credit.status == "PENDING" for credit in self.credits)
        return pending + self.active_second_orders

    def _validate_restored_credits(self) -> None:
        source_keys: set[tuple[str, int]] = set()
        consumed_order_ids: set[int] = set()
        for credit in self.credits:
            if credit.version != TWO_STAGE_VERSION:
                raise ValueError(
                    f"unsupported credit version: {credit.version}"
                )
            source_key = (credit.version, credit.source_order_id)
            if source_key in source_keys:
                raise ValueError(
                    "duplicate (version, source_order_id): "
                    f"{credit.version}, {credit.source_order_id}"
                )
            source_keys.add(source_key)

            if credit.consumed_order_id is None:
                continue
            if credit.consumed_order_id in consumed_order_ids:
                raise ValueError(
                    f"duplicate consumed_order_id: {credit.consumed_order_id}"
                )
            consumed_order_ids.add(credit.consumed_order_id)

    def _resolve_active_second_order_ids(
        self,
        active_second_orders: int | None,
        active_second_order_ids: Iterable[int] | None,
    ) -> set[int] | None:
        restored_ids = None
        if active_second_order_ids is not None:
            restored_id_list = [int(order_id) for order_id in active_second_order_ids]
            if len(restored_id_list) != len(set(restored_id_list)):
                raise ValueError("active_second_order_ids must be unique")
            if any(order_id <= 0 for order_id in restored_id_list):
                raise ValueError("active_second_order_ids must contain positive order IDs")
            restored_ids = set(restored_id_list)

        restored_count = None
        if active_second_orders is not None:
            restored_count = int(active_second_orders)
            if restored_count < 0:
                raise ValueError("active_second_orders must be >= 0")

        if (
            restored_ids is not None
            and restored_count is not None
            and len(restored_ids) != restored_count
        ):
            raise ValueError(
                "active_second_orders conflicts with active_second_order_ids"
            )

        if restored_ids is None and restored_count is not None:
            if restored_count > 0:
                raise ValueError(
                    "active_second_orders > 0 requires active_second_order_ids"
                )
            restored_ids = set()

        if restored_ids is not None and len(restored_ids) > self.max_active:
            raise ValueError("active second-stage orders must not exceed max_active")
        if restored_ids:
            consumed_order_ids = {
                credit.consumed_order_id
                for credit in self.credits
                if credit.version == TWO_STAGE_VERSION
                and credit.status == "CONSUMED"
            }
            missing_ids = sorted(restored_ids - consumed_order_ids)
            if missing_ids:
                raise ValueError(
                    "active_second_order_ids must reference a current-version "
                    f"CONSUMED credit: {missing_ids}"
                )
        return restored_ids

    def _base_terms(self) -> OrderTerms:
        return OrderTerms(
            stake=round(self.base_stake, 4),
            win_return=round(self.base_win_return, 4),
            step=1,
        )

    def _second_terms(self, source_order_id: int | None = None) -> OrderTerms:
        return OrderTerms(
            stake=round(self.second_stake, 4),
            win_return=round(self.second_win_return, 4),
            step=2,
            source_order_id=source_order_id,
        )

    def _resolve_base_return(
        self,
        base_win_return: float | None,
        payout_ratio: float | None,
    ) -> tuple[float, float]:
        if payout_ratio is not None:
            normalized_ratio = self._positive_float(payout_ratio, "payout_ratio")
        else:
            normalized_ratio = None

        if base_win_return is None:
            ratio = 0.8 if normalized_ratio is None else normalized_ratio
            derived_return = self.base_stake * (1.0 + ratio)
            if not math.isfinite(derived_return):
                raise ValueError("base_win_return must be finite")
            return round(derived_return, 4), ratio

        normalized_return = self._positive_float(base_win_return, "base_win_return")
        derived_ratio = (normalized_return / self.base_stake) - 1.0
        if not math.isfinite(derived_ratio) or derived_ratio <= 0:
            raise ValueError(
                "payout_ratio derived from base_win_return must be finite and > 0"
            )
        if normalized_ratio is not None and abs(normalized_ratio - derived_ratio) > 1e-9:
            raise ValueError("payout_ratio conflicts with base_win_return")
        return normalized_return, derived_ratio

    @staticmethod
    def _resolve_alias(
        primary: int | None,
        alias: int | None,
        *,
        default: int,
        primary_name: str,
        alias_name: str,
    ) -> int:
        if primary is not None and alias is not None and int(primary) != int(alias):
            raise ValueError(f"{primary_name} conflicts with {alias_name}")
        if primary is not None:
            return int(primary)
        if alias is not None:
            return int(alias)
        return int(default)

    @staticmethod
    def _positive_float(value: float, name: str) -> float:
        normalized = float(value)
        if not math.isfinite(normalized) or normalized <= 0:
            raise ValueError(f"{name} must be finite and > 0")
        return normalized

    @staticmethod
    def _positive_int(value: int, name: str) -> int:
        normalized = int(value)
        if normalized < 1:
            raise ValueError(f"{name} must be >= 1")
        return normalized

    @staticmethod
    def _positive_order_id(order_id: int) -> int:
        try:
            normalized = int(order_id)
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid order_id: {order_id}") from error
        if normalized < 1:
            raise ValueError("order_id must be >= 1")
        return normalized

    @staticmethod
    def _non_negative_timestamp(value: int, name: str) -> int:
        try:
            normalized = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid {name}: {value}") from error
        if normalized < 0:
            raise ValueError(f"{name} must be >= 0")
        return normalized

    @staticmethod
    def _validate_step(step: int) -> int:
        try:
            normalized = int(step)
        except (TypeError, ValueError) as error:
            raise ValueError(f"unknown step: {step}") from error
        if normalized not in {1, 2}:
            raise ValueError(f"unknown step: {step}")
        return normalized

    @staticmethod
    def _validate_result(result: str) -> str:
        normalized = str(result).upper()
        if normalized not in _RESULTS:
            raise ValueError(f"unknown result: {result}")
        return normalized
