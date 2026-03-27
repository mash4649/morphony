from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from morphony.config import BudgetConfig
from morphony.events import Event, EventBus, EventType
from morphony.models import EscalationLevel, TaskState

if TYPE_CHECKING:
    from morphony.lifecycle import TaskLifecycleManager

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime value must be timezone-aware")
    return value.astimezone(UTC)


def _coerce_now(now: datetime | None) -> datetime:
    if now is None:
        return _utc_now()
    return _ensure_aware(now)


def _empty_usage() -> "BudgetUsage":
    return BudgetUsage()


def _copy_usage(usage: "BudgetUsage") -> "BudgetUsage":
    return BudgetUsage(
        cost_usd=usage.cost_usd,
        elapsed_seconds=usage.elapsed_seconds,
        api_calls=usage.api_calls,
    )


def _remaining_ratio(limit: float, projected_usage: float) -> float:
    if limit < 0:
        raise ValueError("limit must not be negative")
    if projected_usage < 0:
        raise ValueError("projected_usage must not be negative")
    if limit == 0:
        return 1.0 if projected_usage == 0 else 0.0
    ratio = (limit - projected_usage) / limit
    if ratio < 0:
        return 0.0
    if ratio > 1:
        return 1.0
    return ratio


@dataclass(slots=True)
class BudgetUsage:
    cost_usd: float = 0.0
    elapsed_seconds: float = 0.0
    api_calls: int = 0


@dataclass(slots=True)
class BudgetLimits:
    cost_usd: float | None = None
    elapsed_seconds: float | None = None
    api_calls: int | None = None


@dataclass(slots=True)
class BudgetSnapshot:
    task: BudgetUsage
    daily: BudgetUsage
    monthly: BudgetUsage
    task_limits: BudgetLimits
    daily_limits: BudgetLimits
    monthly_limits: BudgetLimits
    day_key: str
    month_key: str


class BudgetControlMode(StrEnum):
    auto_execute = "auto_execute"
    efficiency_mode = "efficiency_mode"
    notify_owner = "notify_owner"
    stop_l3 = "stop_l3"


@dataclass(slots=True)
class BudgetDecision:
    task_id: str
    mode: BudgetControlMode
    escalation_level: EscalationLevel
    remaining_ratio: float
    limiting_scope: str
    limiting_metric: str
    should_notify_owner: bool
    should_stop: bool


class BudgetController:
    def __init__(
        self,
        event_bus: EventBus,
        config: BudgetConfig | None = None,
        lifecycle_manager: "TaskLifecycleManager | None" = None,
    ) -> None:
        self._event_bus = event_bus
        self._config = config if config is not None else BudgetConfig()
        self._lifecycle_manager = lifecycle_manager
        self._task_usage: dict[str, BudgetUsage] = {}
        self._daily_usage: dict[str, BudgetUsage] = {}
        self._monthly_usage: dict[str, BudgetUsage] = {}

    def assess_action(
        self,
        task_id: str,
        *,
        estimated_cost_usd: float = 0.0,
        estimated_elapsed_seconds: float = 0.0,
        estimated_api_calls: int = 0,
        now: datetime | None = None,
    ) -> BudgetDecision:
        task_id = self._ensure_task_id(task_id)
        estimated_cost = self._ensure_non_negative_float("estimated_cost_usd", estimated_cost_usd)
        estimated_elapsed = self._ensure_non_negative_float(
            "estimated_elapsed_seconds",
            estimated_elapsed_seconds,
        )
        estimated_calls = self._ensure_non_negative_int("estimated_api_calls", estimated_api_calls)
        current_time = _coerce_now(now)

        snapshot = self.snapshot(task_id, now=current_time)
        decision = self._derive_decision(
            task_id=task_id,
            task_projected=BudgetUsage(
                cost_usd=snapshot.task.cost_usd + estimated_cost,
                elapsed_seconds=snapshot.task.elapsed_seconds + estimated_elapsed,
                api_calls=snapshot.task.api_calls + estimated_calls,
            ),
            daily_projected=BudgetUsage(
                cost_usd=snapshot.daily.cost_usd + estimated_cost,
                elapsed_seconds=snapshot.daily.elapsed_seconds + estimated_elapsed,
                api_calls=snapshot.daily.api_calls + estimated_calls,
            ),
            monthly_projected=BudgetUsage(
                cost_usd=snapshot.monthly.cost_usd + estimated_cost,
                elapsed_seconds=snapshot.monthly.elapsed_seconds + estimated_elapsed,
                api_calls=snapshot.monthly.api_calls + estimated_calls,
            ),
            task_limits=snapshot.task_limits,
            daily_limits=snapshot.daily_limits,
            monthly_limits=snapshot.monthly_limits,
        )
        return decision

    def enforce_action_budget(
        self,
        task_id: str,
        *,
        estimated_cost_usd: float = 0.0,
        estimated_elapsed_seconds: float = 0.0,
        estimated_api_calls: int = 0,
        now: datetime | None = None,
    ) -> BudgetDecision:
        decision = self.assess_action(
            task_id,
            estimated_cost_usd=estimated_cost_usd,
            estimated_elapsed_seconds=estimated_elapsed_seconds,
            estimated_api_calls=estimated_api_calls,
            now=now,
        )
        current_time = _coerce_now(now)
        self._emit_control_signal(task_id, decision, current_time, phase="pre_action_check")
        if decision.should_stop:
            self._stop_task_best_effort(task_id)
        return decision

    def record_consumption(
        self,
        task_id: str,
        *,
        cost_usd: float,
        elapsed_seconds: float,
        api_calls: int,
        now: datetime | None = None,
        tool_name: str | None = None,
    ) -> BudgetDecision:
        task_id = self._ensure_task_id(task_id)
        cost = self._ensure_non_negative_float("cost_usd", cost_usd)
        elapsed = self._ensure_non_negative_float("elapsed_seconds", elapsed_seconds)
        calls = self._ensure_non_negative_int("api_calls", api_calls)
        current_time = _coerce_now(now)
        day_key = self._day_key(current_time)
        month_key = self._month_key(current_time)

        task_usage = self._task_usage.setdefault(task_id, _empty_usage())
        daily_usage = self._daily_usage.setdefault(day_key, _empty_usage())
        monthly_usage = self._monthly_usage.setdefault(month_key, _empty_usage())

        self._apply_consumption(task_usage, cost, elapsed, calls)
        self._apply_consumption(daily_usage, cost, elapsed, calls)
        self._apply_consumption(monthly_usage, cost, elapsed, calls)

        snapshot = self.snapshot(task_id, now=current_time)
        decision = self._derive_decision(
            task_id=task_id,
            task_projected=snapshot.task,
            daily_projected=snapshot.daily,
            monthly_projected=snapshot.monthly,
            task_limits=snapshot.task_limits,
            daily_limits=snapshot.daily_limits,
            monthly_limits=snapshot.monthly_limits,
        )
        self._publish_budget_consumed_event(
            task_id=task_id,
            decision=decision,
            snapshot=snapshot,
            consumed=BudgetUsage(cost_usd=cost, elapsed_seconds=elapsed, api_calls=calls),
            timestamp=current_time,
            tool_name=tool_name,
        )
        self._emit_control_signal(task_id, decision, current_time, phase="post_consumption")
        if decision.should_stop:
            self._stop_task_best_effort(task_id)
        return decision

    def record_tool_call(
        self,
        task_id: str,
        *,
        tool_name: str,
        cost_per_call_usd: float,
        elapsed_seconds: float,
        api_calls: int = 1,
        now: datetime | None = None,
    ) -> BudgetDecision:
        task_id = self._ensure_task_id(task_id)
        if not tool_name:
            raise ValueError("tool_name must not be empty")
        cost_per_call = self._ensure_non_negative_float("cost_per_call_usd", cost_per_call_usd)
        calls = self._ensure_non_negative_int("api_calls", api_calls)
        total_cost = cost_per_call * float(calls)
        return self.record_consumption(
            task_id,
            cost_usd=total_cost,
            elapsed_seconds=elapsed_seconds,
            api_calls=calls,
            now=now,
            tool_name=tool_name,
        )

    def snapshot(
        self,
        task_id: str,
        *,
        now: datetime | None = None,
    ) -> BudgetSnapshot:
        task_id = self._ensure_task_id(task_id)
        current_time = _coerce_now(now)
        day_key = self._day_key(current_time)
        month_key = self._month_key(current_time)

        task_usage = _copy_usage(self._task_usage.get(task_id, BudgetUsage()))
        daily_usage = _copy_usage(self._daily_usage.get(day_key, BudgetUsage()))
        monthly_usage = _copy_usage(self._monthly_usage.get(month_key, BudgetUsage()))
        return BudgetSnapshot(
            task=task_usage,
            daily=daily_usage,
            monthly=monthly_usage,
            task_limits=self._task_limits(),
            daily_limits=self._daily_limits(),
            monthly_limits=self._monthly_limits(),
            day_key=day_key,
            month_key=month_key,
        )

    def _derive_decision(
        self,
        *,
        task_id: str,
        task_projected: BudgetUsage,
        daily_projected: BudgetUsage,
        monthly_projected: BudgetUsage,
        task_limits: BudgetLimits,
        daily_limits: BudgetLimits,
        monthly_limits: BudgetLimits,
    ) -> BudgetDecision:
        ratios: list[tuple[float, str, str]] = []
        ratios.extend(
            self._collect_ratios("task", task_projected, task_limits)
        )
        ratios.extend(
            self._collect_ratios("daily", daily_projected, daily_limits)
        )
        ratios.extend(
            self._collect_ratios("monthly", monthly_projected, monthly_limits)
        )

        if not ratios:
            return BudgetDecision(
                task_id=task_id,
                mode=BudgetControlMode.auto_execute,
                escalation_level=EscalationLevel.L1,
                remaining_ratio=1.0,
                limiting_scope="none",
                limiting_metric="none",
                should_notify_owner=False,
                should_stop=False,
            )

        remaining_ratio, limiting_scope, limiting_metric = min(ratios, key=lambda item: item[0])
        mode, escalation_level = self._mode_from_ratio(remaining_ratio)
        should_notify_owner = mode is BudgetControlMode.notify_owner
        should_stop = mode is BudgetControlMode.stop_l3
        return BudgetDecision(
            task_id=task_id,
            mode=mode,
            escalation_level=escalation_level,
            remaining_ratio=remaining_ratio,
            limiting_scope=limiting_scope,
            limiting_metric=limiting_metric,
            should_notify_owner=should_notify_owner,
            should_stop=should_stop,
        )

    def _collect_ratios(
        self,
        scope: str,
        usage: BudgetUsage,
        limits: BudgetLimits,
    ) -> list[tuple[float, str, str]]:
        ratios: list[tuple[float, str, str]] = []
        if limits.cost_usd is not None:
            ratios.append(
                (
                    _remaining_ratio(limits.cost_usd, usage.cost_usd),
                    scope,
                    "cost_usd",
                )
            )
        if limits.elapsed_seconds is not None:
            ratios.append(
                (
                    _remaining_ratio(limits.elapsed_seconds, usage.elapsed_seconds),
                    scope,
                    "elapsed_seconds",
                )
            )
        if limits.api_calls is not None:
            ratios.append(
                (
                    _remaining_ratio(float(limits.api_calls), float(usage.api_calls)),
                    scope,
                    "api_calls",
                )
            )
        return ratios

    def _mode_from_ratio(self, remaining_ratio: float) -> tuple[BudgetControlMode, EscalationLevel]:
        if remaining_ratio > 0.5:
            return BudgetControlMode.auto_execute, EscalationLevel.L1
        if remaining_ratio >= 0.2:
            return BudgetControlMode.efficiency_mode, EscalationLevel.L1
        if remaining_ratio >= 0.05:
            return BudgetControlMode.notify_owner, EscalationLevel.L2
        return BudgetControlMode.stop_l3, EscalationLevel.L3

    def _task_limits(self) -> BudgetLimits:
        return BudgetLimits(
            cost_usd=self._config.task.cost_usd,
            elapsed_seconds=float(self._config.task.time_minutes) * 60.0,
            api_calls=self._config.task.api_calls,
        )

    def _daily_limits(self) -> BudgetLimits:
        return BudgetLimits(
            cost_usd=self._config.daily.cost_usd,
            elapsed_seconds=float(self._config.daily.time_hours) * 3600.0,
            api_calls=None,
        )

    def _monthly_limits(self) -> BudgetLimits:
        return BudgetLimits(
            cost_usd=self._config.monthly.cost_usd,
            elapsed_seconds=None,
            api_calls=None,
        )

    def _publish_budget_consumed_event(
        self,
        *,
        task_id: str,
        decision: BudgetDecision,
        snapshot: BudgetSnapshot,
        consumed: BudgetUsage,
        timestamp: datetime,
        tool_name: str | None,
    ) -> None:
        payload: dict[str, object] = {
            "source": "budget_controller",
            "mode": decision.mode.value,
            "level": decision.escalation_level.value,
            "remaining_ratio": decision.remaining_ratio,
            "limiting_scope": decision.limiting_scope,
            "limiting_metric": decision.limiting_metric,
            "should_notify_owner": decision.should_notify_owner,
            "should_stop": decision.should_stop,
            "consumed": {
                "cost_usd": consumed.cost_usd,
                "elapsed_seconds": consumed.elapsed_seconds,
                "api_calls": consumed.api_calls,
            },
            "totals": {
                "task": {
                    "cost_usd": snapshot.task.cost_usd,
                    "elapsed_seconds": snapshot.task.elapsed_seconds,
                    "api_calls": snapshot.task.api_calls,
                },
                "daily": {
                    "cost_usd": snapshot.daily.cost_usd,
                    "elapsed_seconds": snapshot.daily.elapsed_seconds,
                    "api_calls": snapshot.daily.api_calls,
                    "day_key": snapshot.day_key,
                },
                "monthly": {
                    "cost_usd": snapshot.monthly.cost_usd,
                    "elapsed_seconds": snapshot.monthly.elapsed_seconds,
                    "api_calls": snapshot.monthly.api_calls,
                    "month_key": snapshot.month_key,
                },
            },
        }
        if tool_name is not None:
            payload["tool_name"] = tool_name
        self._event_bus.publish_sync(
            Event(
                task_id=task_id,
                event_type=EventType.budget_consumed,
                timestamp=timestamp,
                payload=payload,
            )
        )

    def _emit_control_signal(
        self,
        task_id: str,
        decision: BudgetDecision,
        timestamp: datetime,
        *,
        phase: str,
    ) -> None:
        if decision.mode not in {BudgetControlMode.notify_owner, BudgetControlMode.stop_l3}:
            return
        reason = (
            "budget_remaining_below_5_percent"
            if decision.mode is BudgetControlMode.stop_l3
            else "budget_remaining_between_5_and_20_percent"
        )
        self._event_bus.publish_sync(
            Event(
                task_id=task_id,
                event_type=EventType.escalation_triggered,
                timestamp=timestamp,
                payload={
                    "source": "budget_controller",
                    "phase": phase,
                    "reason": reason,
                    "level": decision.escalation_level.value,
                    "mode": decision.mode.value,
                    "remaining_ratio": decision.remaining_ratio,
                    "limiting_scope": decision.limiting_scope,
                    "limiting_metric": decision.limiting_metric,
                },
            )
        )

    def _stop_task_best_effort(self, task_id: str) -> None:
        if self._lifecycle_manager is None:
            return
        try:
            current_state = self._lifecycle_manager.get_task_state(task_id)
        except KeyError:
            return
        if current_state in {TaskState.stopped, TaskState.completed, TaskState.failed}:
            return
        try:
            self._lifecycle_manager.transition(task_id, TaskState.stopped)
        except Exception as exc:
            logger.warning("Failed to stop task %s after budget exhaustion: %s", task_id, exc)

    def _apply_consumption(
        self,
        usage: BudgetUsage,
        cost_usd: float,
        elapsed_seconds: float,
        api_calls: int,
    ) -> None:
        usage.cost_usd += cost_usd
        usage.elapsed_seconds += elapsed_seconds
        usage.api_calls += api_calls

    def _day_key(self, now: datetime) -> str:
        return now.date().isoformat()

    def _month_key(self, now: datetime) -> str:
        return f"{now.year:04d}-{now.month:02d}"

    def _ensure_task_id(self, task_id: str) -> str:
        if not task_id:
            raise ValueError("task_id must not be empty")
        return task_id

    def _ensure_non_negative_float(self, name: str, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be a number")
        normalized = float(value)
        if normalized < 0:
            raise ValueError(f"{name} must not be negative")
        return normalized

    def _ensure_non_negative_int(self, name: str, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
        if value < 0:
            raise ValueError(f"{name} must not be negative")
        return value


__all__ = [
    "BudgetControlMode",
    "BudgetController",
    "BudgetDecision",
    "BudgetLimits",
    "BudgetSnapshot",
    "BudgetUsage",
]
