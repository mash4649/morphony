from __future__ import annotations

import logging
from pathlib import Path

import pytest

from morphony.events import Event, EventBus, EventType
from morphony.lifecycle import (
    CheckpointCorruptedError,
    CheckpointManager,
    FailureClass,
    TaskLifecycleManager,
)
from morphony.models import TaskState


def test_checkpoint_is_persisted_after_each_step(tmp_path: Path) -> None:
    bus = EventBus()
    checkpoint_events: list[Event] = []
    bus.subscribe(EventType.checkpoint_saved, checkpoint_events.append)

    manager = CheckpointManager(tmp_path / "checkpoints", event_bus=bus)
    task_id = "task-1"

    manager.save_step_completion(
        task_id=task_id,
        step_id="step-1",
        artifacts=["artifact-1.md"],
        budget_delta={"api_calls": 1},
    )
    manager.save_step_completion(
        task_id=task_id,
        step_id="step-2",
        artifacts=["artifact-2.md"],
        budget_delta={"api_calls": 2},
    )

    checkpoint = manager.load_checkpoint(task_id)
    assert checkpoint is not None
    assert checkpoint.version >= 1
    assert checkpoint.completed_steps == ["step-1", "step-2"]
    assert checkpoint.last_completed_step_id == "step-2"
    assert checkpoint.budget_delta["api_calls"] == 3
    assert manager.checkpoint_file_for_task(task_id).exists()

    assert len(checkpoint_events) == 2
    assert all(event.event_type == EventType.checkpoint_saved for event in checkpoint_events)


def test_transient_retry_escalates_after_three_attempts(tmp_path: Path) -> None:
    manager = CheckpointManager(tmp_path / "checkpoints")
    task_id = "task-2"
    step_id = "step-retry"

    first = manager.handle_failure(task_id, step_id, FailureClass.transient, "t1")
    second = manager.handle_failure(task_id, step_id, FailureClass.transient, "t2")
    third = manager.handle_failure(task_id, step_id, FailureClass.transient, "t3")
    fourth = manager.handle_failure(task_id, step_id, FailureClass.transient, "t4")

    assert first.classification == FailureClass.transient
    assert first.action == "retry"
    assert first.retry_delay_seconds == 1
    assert first.attempt == 1

    assert second.classification == FailureClass.transient
    assert second.retry_delay_seconds == 2
    assert second.attempt == 2

    assert third.classification == FailureClass.transient
    assert third.retry_delay_seconds == 4
    assert third.attempt == 3

    assert fourth.classification == FailureClass.permanent
    assert fourth.action == "skip_step_and_try_alternative"
    assert fourth.promoted_to_permanent is True
    assert fourth.attempt == 4


def test_fatal_failure_preserves_artifacts_emits_l3_and_stops_task(tmp_path: Path) -> None:
    bus = EventBus()
    escalation_events: list[Event] = []
    bus.subscribe(EventType.escalation_triggered, escalation_events.append)

    lifecycle = TaskLifecycleManager(tmp_path / "lifecycle.json", event_bus=bus)
    task_id = "task-fatal"
    lifecycle.submit_task(task_id)
    assert lifecycle.get_task_state(task_id) == TaskState.running

    manager = CheckpointManager(
        tmp_path / "checkpoints",
        event_bus=bus,
        lifecycle_manager=lifecycle,
    )
    decision = manager.handle_failure(
        task_id,
        "step-fatal",
        FailureClass.fatal,
        "fatal error",
        artifacts=["partial.md"],
    )

    checkpoint = manager.load_checkpoint(task_id)
    assert checkpoint is not None
    assert "partial.md" in checkpoint.partial_artifacts
    assert decision.l3_escalation is True
    assert decision.preserve_partial_artifacts is True
    assert lifecycle.get_task_state(task_id) == TaskState.stopped

    assert escalation_events
    assert all(event.event_type == EventType.escalation_triggered for event in escalation_events)


def test_resume_logs_checkpoint_position(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    manager = CheckpointManager(tmp_path / "checkpoints")
    task_id = "task-resume"

    manager.save_step_completion(task_id, "step-1")
    manager.save_step_completion(task_id, "step-2")
    resume = manager.resume_task(task_id)

    assert resume is not None
    assert resume.resume_after_step_id == "step-2"
    assert any("Resuming task" in record.message for record in caplog.records)
    assert any("step-2" in record.message for record in caplog.records)


def test_corrupted_checkpoint_raises_error(tmp_path: Path) -> None:
    manager = CheckpointManager(tmp_path / "checkpoints")
    task_id = "task-corrupt"
    manager.save_step_completion(task_id, "step-1")

    checkpoint_path = manager.checkpoint_file_for_task(task_id)
    checkpoint_path.write_text("{invalid json", encoding="utf-8")

    with pytest.raises(CheckpointCorruptedError):
        manager.load_checkpoint(task_id)

