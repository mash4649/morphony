from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from morphony.config import (
    AgentConfig,
    BudgetConfig,
    BudgetDailyConfig,
    BudgetMonthlyConfig,
    BudgetTaskConfig,
)
from morphony.events import Event, EventBus, EventType
from morphony.lifecycle import TaskLifecycleManager
from morphony.models import TaskState
from morphony.safety import BudgetControlMode, BudgetController


def _assert_close(actual: float, expected: float, tolerance: float = 1e-9) -> None:
    assert abs(actual - expected) <= tolerance


def _as_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _as_float(value: object) -> float:
    assert isinstance(value, (int, float))
    assert not isinstance(value, bool)
    return float(value)


def _build_budget_stack(
    tmp_path: Path,
    *,
    config: BudgetConfig | None = None,
) -> tuple[BudgetController, TaskLifecycleManager, EventBus]:
    bus = EventBus()
    lifecycle = TaskLifecycleManager(tmp_path / "lifecycle.json", event_bus=bus)
    controller = BudgetController(
        event_bus=bus,
        config=config if config is not None else AgentConfig().budget,
        lifecycle_manager=lifecycle,
    )
    return controller, lifecycle, bus


def _test_budget_config(limit: float = 100.0) -> BudgetConfig:
    return BudgetConfig(
        task=BudgetTaskConfig(cost_usd=limit, time_minutes=60, api_calls=100),
        daily=BudgetDailyConfig(cost_usd=limit, time_hours=24.0),
        monthly=BudgetMonthlyConfig(cost_usd=limit),
    )


def test_record_consumption_tracks_task_daily_and_monthly_and_emits_budget_consumed_events(
    tmp_path: Path,
) -> None:
    controller, lifecycle, bus = _build_budget_stack(tmp_path, config=_test_budget_config())
    task_id = "task-budget-tracking"
    lifecycle.submit_task(task_id)

    events: list[Event] = []
    bus.subscribe_all(events.append)

    base = datetime(2026, 3, 26, 12, 0, tzinfo=UTC)

    first_decision = controller.record_consumption(
        task_id,
        cost_usd=7.5,
        elapsed_seconds=12.0,
        api_calls=3,
        now=base,
        tool_name="search",
    )
    second_decision = controller.record_tool_call(
        task_id,
        tool_name="lookup",
        cost_per_call_usd=1.75,
        elapsed_seconds=8.0,
        api_calls=4,
        now=base + timedelta(minutes=1),
    )

    snapshot = controller.snapshot(task_id, now=base + timedelta(minutes=1))

    assert first_decision.mode == BudgetControlMode.auto_execute
    assert second_decision.mode == BudgetControlMode.auto_execute

    _assert_close(snapshot.task.cost_usd, 14.5)
    _assert_close(snapshot.task.elapsed_seconds, 20.0)
    assert snapshot.task.api_calls == 7
    _assert_close(snapshot.daily.cost_usd, 14.5)
    _assert_close(snapshot.daily.elapsed_seconds, 20.0)
    assert snapshot.daily.api_calls == 7
    _assert_close(snapshot.monthly.cost_usd, 14.5)
    _assert_close(snapshot.monthly.elapsed_seconds, 20.0)
    assert snapshot.monthly.api_calls == 7
    assert snapshot.day_key == "2026-03-26"
    assert snapshot.month_key == "2026-03"

    assert len(events) == 2
    assert all(event.event_type == EventType.budget_consumed for event in events)
    assert events[0].payload["consumed"] == {
        "cost_usd": 7.5,
        "elapsed_seconds": 12.0,
        "api_calls": 3,
    }
    totals_0 = _as_dict(events[0].payload["totals"])
    task_totals_0 = _as_dict(totals_0["task"])
    _assert_close(_as_float(task_totals_0["cost_usd"]), 7.5)
    assert events[0].payload["totals"]["daily"]["day_key"] == "2026-03-26"
    assert events[0].payload["totals"]["monthly"]["month_key"] == "2026-03"
    assert events[1].payload["tool_name"] == "lookup"
    consumed_1 = _as_dict(events[1].payload["consumed"])
    _assert_close(_as_float(consumed_1["cost_usd"]), 7.0)
    totals_1 = _as_dict(events[1].payload["totals"])
    task_totals_1 = _as_dict(totals_1["task"])
    _assert_close(_as_float(task_totals_1["cost_usd"]), 14.5)


@pytest.mark.parametrize(
    ("estimated_cost_usd", "expected_mode", "should_notify_owner", "should_stop"),
    [
        (40.0, BudgetControlMode.auto_execute, False, False),
        (50.0, BudgetControlMode.efficiency_mode, False, False),
        (79.0, BudgetControlMode.efficiency_mode, False, False),
        (85.0, BudgetControlMode.notify_owner, True, False),
        (96.0, BudgetControlMode.stop_l3, False, True),
    ],
)
def test_assess_action_switches_modes_across_budget_ranges(
    tmp_path: Path,
    estimated_cost_usd: float,
    expected_mode: BudgetControlMode,
    should_notify_owner: bool,
    should_stop: bool,
) -> None:
    controller, lifecycle, _ = _build_budget_stack(tmp_path, config=_test_budget_config())
    task_id = "task-thresholds"
    lifecycle.submit_task(task_id)

    decision = controller.assess_action(
        task_id,
        estimated_cost_usd=estimated_cost_usd,
        now=datetime(2026, 3, 26, 12, 0, tzinfo=UTC),
    )

    assert decision.mode == expected_mode
    assert decision.should_notify_owner is should_notify_owner
    assert decision.should_stop is should_stop
    _assert_close(decision.remaining_ratio, (100.0 - estimated_cost_usd) / 100.0)
    assert decision.limiting_scope == "task"
    assert decision.limiting_metric == "cost_usd"


def test_enforce_action_budget_stops_task_below_five_percent(
    tmp_path: Path,
) -> None:
    controller, lifecycle, bus = _build_budget_stack(tmp_path, config=_test_budget_config())
    task_id = "task-stop"
    lifecycle.submit_task(task_id)

    state_events: list[Event] = []
    escalation_events: list[Event] = []
    bus.subscribe(EventType.state_changed, state_events.append)
    bus.subscribe(EventType.escalation_triggered, escalation_events.append)

    decision = controller.enforce_action_budget(
        task_id,
        estimated_cost_usd=96.0,
        now=datetime(2026, 3, 26, 12, 0, tzinfo=UTC),
    )

    assert decision.mode == BudgetControlMode.stop_l3
    assert decision.should_stop is True
    assert lifecycle.get_task_state(task_id) == TaskState.stopped

    assert len(escalation_events) == 1
    assert escalation_events[0].event_type == EventType.escalation_triggered
    assert escalation_events[0].payload["source"] == "budget_controller"
    assert escalation_events[0].payload["phase"] == "pre_action_check"
    assert escalation_events[0].payload["reason"] == "budget_remaining_below_5_percent"

    assert any(
        event.event_type == EventType.state_changed
        and event.payload["from_state"] == TaskState.running.value
        and event.payload["to_state"] == TaskState.stopped.value
        for event in state_events
    )
