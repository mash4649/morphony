from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from morphony.events import Event, EventBus, EventType
from morphony.lifecycle import InvalidTransitionError, TaskLifecycleManager
from morphony.models import TaskState


def _run(value: object) -> object:
    if asyncio.iscoroutine(value):
        return asyncio.run(value)
    return value


def _task_id_from_submission(value: object) -> str:
    if isinstance(value, str):
        return value

    task_id = getattr(value, "task_id", None)
    if isinstance(task_id, str):
        return task_id

    pytest.fail(f"submit_task returned unsupported value: {value!r}")


def _valid_target_for_state(state: TaskState) -> TaskState:
    if state == TaskState.pending:
        return TaskState.planning
    if state == TaskState.planning:
        return TaskState.approved
    if state == TaskState.approved:
        return TaskState.running
    if state == TaskState.running:
        return TaskState.completed
    if state == TaskState.paused:
        return TaskState.running

    pytest.fail(f"unexpected initial state for lifecycle test: {state}")


def _transition_sequence_for_state(state: TaskState) -> list[TaskState]:
    if state == TaskState.pending:
        return [TaskState.planning, TaskState.approved, TaskState.running]
    if state == TaskState.planning:
        return [TaskState.approved, TaskState.running, TaskState.completed]
    if state == TaskState.approved:
        return [TaskState.running, TaskState.completed]
    if state == TaskState.running:
        return [TaskState.paused, TaskState.running, TaskState.completed]
    if state == TaskState.paused:
        return [TaskState.running, TaskState.completed]

    pytest.fail(f"unexpected initial state for lifecycle test: {state}")


def test_valid_and_invalid_transitions_emit_state_changed(tmp_path: Path) -> None:
    bus = EventBus()
    captured_events: list[Event] = []
    bus.subscribe(EventType.state_changed, captured_events.append)

    store_path = tmp_path / "lifecycle.db"
    manager = TaskLifecycleManager(store_path, event_bus=bus)

    task_id = _task_id_from_submission(_run(manager.submit_task("lifecycle transition coverage")))
    assert task_id

    initial_state = manager.get_task_state(task_id)
    sequence = _transition_sequence_for_state(initial_state)
    captured_events.clear()

    current_state = initial_state
    for target_state in sequence:
        _run(manager.transition(task_id, target_state))
        current_state = target_state
        assert manager.get_task_state(task_id) == target_state

    assert len(captured_events) == len(sequence)
    assert {event.event_type for event in captured_events} == {EventType.state_changed}
    assert {event.task_id for event in captured_events} == {task_id}

    with pytest.raises(InvalidTransitionError):
        _run(manager.transition(task_id, TaskState.pending))
    assert manager.get_task_state(task_id) == current_state


def test_state_and_history_are_restored_after_restart(tmp_path: Path) -> None:
    store_path = tmp_path / "lifecycle.db"
    manager = TaskLifecycleManager(store_path)

    task_id = _task_id_from_submission(_run(manager.submit_task("persisted lifecycle state")))
    initial_state = manager.get_task_state(task_id)
    target_state = _valid_target_for_state(initial_state)

    _run(manager.transition(task_id, target_state))

    state_before_restart = manager.get_task_state(task_id)
    history_before_restart = list(manager.get_transition_history(task_id))
    pending_before_restart = list(manager.pending_queue)
    running_before_restart = manager.running_task_id

    reloaded = TaskLifecycleManager(store_path)

    assert reloaded.get_task_state(task_id) == state_before_restart
    assert reloaded.get_transition_history(task_id) == history_before_restart
    assert list(reloaded.pending_queue) == pending_before_restart
    assert reloaded.running_task_id == running_before_restart


def test_second_task_waits_pending_then_autostarts_after_first_completion(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "lifecycle.db"
    manager = TaskLifecycleManager(store_path)

    first_task_id = _task_id_from_submission(_run(manager.submit_task("first task")))
    second_task_id = _task_id_from_submission(_run(manager.submit_task("second task")))

    assert manager.running_task_id == first_task_id
    assert manager.get_task_state(first_task_id) == TaskState.running
    assert manager.get_task_state(second_task_id) == TaskState.pending
    assert list(manager.pending_queue) == [second_task_id]

    _run(manager.transition(first_task_id, TaskState.completed))

    assert manager.get_task_state(first_task_id) == TaskState.completed
    assert manager.running_task_id == second_task_id
    assert manager.get_task_state(second_task_id) == TaskState.running
    assert list(manager.pending_queue) == []
