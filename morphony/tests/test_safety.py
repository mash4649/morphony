from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from morphony.events import AuditLogReader, AuditLogWriter, Event, EventBus, EventType
from morphony.lifecycle import CheckpointManager, TaskLifecycleManager
from morphony.models import TaskState
from morphony.safety import SafetyController


def _build_safety_stack(tmp_path: Path) -> tuple[
    SafetyController,
    TaskLifecycleManager,
    CheckpointManager,
    EventBus,
    Path,
]:
    bus = EventBus()
    lifecycle = TaskLifecycleManager(tmp_path / "lifecycle.json", event_bus=bus)
    checkpoint = CheckpointManager(
        tmp_path / "checkpoints",
        event_bus=bus,
        lifecycle_manager=lifecycle,
    )
    audit_log_path = tmp_path / "audit.log"
    audit_log_writer = AuditLogWriter(audit_log_path)
    controller = SafetyController(
        event_bus=bus,
        lifecycle_manager=lifecycle,
        checkpoint_manager=checkpoint,
        audit_log_writer=audit_log_writer,
    )
    return controller, lifecycle, checkpoint, bus, audit_log_path


def test_stop_task_transitions_to_stopped_and_preserves_checkpoint_and_audit_log(
    tmp_path: Path,
) -> None:
    controller, lifecycle, checkpoint, bus, audit_log_path = _build_safety_stack(
        tmp_path
    )
    state_events: list[Event] = []
    checkpoint_events: list[Event] = []
    bus.subscribe(EventType.state_changed, state_events.append)
    bus.subscribe(EventType.checkpoint_saved, checkpoint_events.append)

    task_id = "task-stop"
    lifecycle.submit_task(task_id)
    assert lifecycle.get_task_state(task_id) == TaskState.running

    controller.stop_task(task_id, artifacts=["draft.md"])

    assert lifecycle.get_task_state(task_id) == TaskState.stopped

    checkpoint_data = checkpoint.load_checkpoint(task_id)
    assert checkpoint_data is not None
    assert checkpoint_data.completed_steps == ["safety_stop_owner_kill_switch"]
    assert checkpoint_data.last_completed_step_id == "safety_stop_owner_kill_switch"
    assert "safety_stop_owner_kill_switch" in checkpoint_data.step_records

    step_record = checkpoint_data.step_records["safety_stop_owner_kill_switch"]
    assert step_record.status == "completed"
    assert step_record.artifacts == ["draft.md"]
    assert checkpoint.checkpoint_file_for_task(task_id).exists()

    assert len(checkpoint_events) == 1
    assert checkpoint_events[0].event_type == EventType.checkpoint_saved
    assert checkpoint_events[0].payload["step_id"] == "safety_stop_owner_kill_switch"
    assert checkpoint_events[0].payload["artifacts"] == ["draft.md"]

    assert any(
        event.task_id == task_id
        and event.payload["to_state"] == TaskState.stopped.value
        for event in state_events
    )

    reader = AuditLogReader(audit_log_path)
    audit_events = reader.read(task_id=task_id, event_type=EventType.state_changed)
    assert len(audit_events) == 1
    assert audit_events[0].payload["source"] == "safety_controller"
    assert audit_events[0].payload["reason"] == "owner_kill_switch"
    assert audit_events[0].payload["to_state"] == TaskState.stopped.value


def test_cost_spike_auto_stops_and_emits_escalation_triggered(
    tmp_path: Path,
) -> None:
    controller, lifecycle, checkpoint, bus, _ = _build_safety_stack(tmp_path)
    escalation_events: list[Event] = []
    bus.subscribe(EventType.escalation_triggered, escalation_events.append)

    task_id = "task-cost-spike"
    lifecycle.submit_task(task_id)
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    for offset in range(5):
        controller.record_action(
            task_id,
            tool_name="tool-a",
            tool_input=f"input-{offset}",
            cost=10.0,
            now=base + timedelta(minutes=offset),
        )
        assert lifecycle.get_task_state(task_id) == TaskState.running

    controller.record_action(
        task_id,
        tool_name="tool-a",
        tool_input="input-spike",
        cost=31.0,
        now=base + timedelta(minutes=5),
    )

    assert lifecycle.get_task_state(task_id) == TaskState.stopped
    assert len(escalation_events) == 1

    event = escalation_events[0]
    assert event.event_type == EventType.escalation_triggered
    assert event.payload["reason"] == "auto_stop_cost_spike"
    assert event.payload["anomaly_types"] == ["cost_spike"]
    assert event.payload["tool_name"] == "tool-a"
    assert event.payload["tool_input"] == "input-spike"
    assert event.payload["cost_window_average"] == 10.0
    assert event.payload["cost_spike_multiplier"] == 3.0

    checkpoint_data = checkpoint.load_checkpoint(task_id)
    assert checkpoint_data is not None
    assert "safety_stop_auto_stop_cost_spike" in checkpoint_data.completed_steps


def test_repeat_same_tool_and_input_auto_stops_and_emits_escalation_triggered(
    tmp_path: Path,
) -> None:
    controller, lifecycle, checkpoint, bus, _ = _build_safety_stack(tmp_path)
    escalation_events: list[Event] = []
    bus.subscribe(EventType.escalation_triggered, escalation_events.append)

    task_id = "task-repeat"
    lifecycle.submit_task(task_id)
    base = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)

    for offset in range(4):
        controller.record_action(
            task_id,
            tool_name="tool-b",
            tool_input="same-input",
            cost=1.0,
            now=base + timedelta(minutes=offset),
        )
        assert lifecycle.get_task_state(task_id) == TaskState.running
        assert escalation_events == []

    controller.record_action(
        task_id,
        tool_name="tool-b",
        tool_input="same-input",
        cost=1.0,
        now=base + timedelta(minutes=4),
    )

    assert lifecycle.get_task_state(task_id) == TaskState.stopped
    assert len(escalation_events) == 1

    event = escalation_events[0]
    assert event.event_type == EventType.escalation_triggered
    assert event.payload["reason"] == "auto_stop_repeat_loop"
    assert event.payload["anomaly_types"] == ["repeat_loop"]
    assert event.payload["tool_name"] == "tool-b"
    assert event.payload["tool_input"] == "same-input"
    assert event.payload["repeat_count"] == 5
    assert event.payload["repeat_threshold"] == 5

    checkpoint_data = checkpoint.load_checkpoint(task_id)
    assert checkpoint_data is not None
    assert "safety_stop_auto_stop_repeat_loop" in checkpoint_data.completed_steps
