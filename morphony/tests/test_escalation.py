from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from morphony.config import AgentConfig
from morphony.events import Event, EventBus, EventType
from morphony.lifecycle import CheckpointManager, TaskLifecycleManager
from morphony.models import EscalationLevel, TaskState
from morphony.safety import EscalationEngine, EscalationRequestStatus


def _setup_engine(
    tmp_path: Path,
) -> tuple[EscalationEngine, EventBus, TaskLifecycleManager, CheckpointManager]:
    bus = EventBus()
    lifecycle = TaskLifecycleManager(tmp_path / "lifecycle.json", event_bus=bus)
    checkpoint = CheckpointManager(tmp_path / "checkpoints", event_bus=bus)
    config = AgentConfig().escalation
    engine = EscalationEngine(
        event_bus=bus,
        lifecycle_manager=lifecycle,
        checkpoint_manager=checkpoint,
        config=config,
    )
    return engine, bus, lifecycle, checkpoint


def test_classify_action_maps_search_to_l1_and_finalize_to_l3(tmp_path: Path) -> None:
    engine, _, _, _ = _setup_engine(tmp_path)
    assert engine.classify_action("web_search") == EscalationLevel.L1
    assert engine.classify_action("finalize_report") == EscalationLevel.L3


def test_classify_action_fail_safe_defaults_to_l3(tmp_path: Path) -> None:
    engine, _, _, _ = _setup_engine(tmp_path)
    assert engine.classify_action("custom_unknown_action") == EscalationLevel.L3


def test_l2_timeout_policy_escalate_moves_request_to_l3_waiting_approval(tmp_path: Path) -> None:
    engine, bus, lifecycle, _ = _setup_engine(tmp_path)
    events: list[Event] = []
    bus.subscribe(EventType.escalation_triggered, events.append)

    task_id = "task-l2"
    lifecycle.submit_task(task_id)
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    record = engine.request_escalation(
        task_id=task_id,
        action_name="adjust_plan",
        context={"needs_review": True},
        now=base,
    )
    assert record.level == EscalationLevel.L2
    assert record.status == EscalationRequestStatus.notified

    updated = engine.process_timeouts(now=base + timedelta(minutes=16))
    assert updated
    escalated = updated[-1]
    assert escalated.level == EscalationLevel.L3
    assert escalated.status == EscalationRequestStatus.waiting_approval

    assert events
    assert all(event.event_type == EventType.escalation_triggered for event in events)


def test_l3_waiting_approval_reminds_and_auto_suspends_after_timeout(tmp_path: Path) -> None:
    engine, bus, lifecycle, checkpoint = _setup_engine(tmp_path)
    events: list[Event] = []
    bus.subscribe(EventType.escalation_triggered, events.append)

    task_id = "task-l3"
    lifecycle.submit_task(task_id)
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    record = engine.request_escalation(
        task_id=task_id,
        action_name="finalize_report",
        context={"partial_artifacts": ["draft.md"]},
        now=base,
    )
    assert record.level == EscalationLevel.L3
    assert record.status == EscalationRequestStatus.waiting_approval

    reminder_updates = engine.process_timeouts(now=base + timedelta(minutes=30))
    assert reminder_updates
    assert reminder_updates[-1].status == EscalationRequestStatus.waiting_approval
    assert reminder_updates[-1].reminder_sent_at is not None

    suspend_updates = engine.process_timeouts(now=base + timedelta(hours=24, seconds=1))
    assert suspend_updates
    suspended = suspend_updates[-1]
    assert suspended.status == EscalationRequestStatus.suspended
    assert lifecycle.get_task_state(task_id) == TaskState.suspended

    preserved = checkpoint.load_checkpoint(task_id)
    assert preserved is not None
    assert any(step.startswith("l3_auto_suspend_") for step in preserved.completed_steps)
    assert events
    assert all(event.event_type == EventType.escalation_triggered for event in events)


def test_all_escalations_publish_escalation_triggered_events(tmp_path: Path) -> None:
    engine, bus, lifecycle, _ = _setup_engine(tmp_path)
    task_id = "task-events"
    lifecycle.submit_task(task_id)
    events: list[Event] = []
    bus.subscribe(EventType.escalation_triggered, events.append)

    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    engine.request_escalation(task_id, "web_search", now=base)
    engine.request_escalation(task_id, "adjust_plan", context={"needs_review": True}, now=base)
    engine.process_timeouts(now=base + timedelta(minutes=16))
    engine.request_escalation(task_id, "finalize_report", now=base + timedelta(minutes=20))
    engine.process_timeouts(now=base + timedelta(minutes=50))
    engine.process_timeouts(now=base + timedelta(hours=24, seconds=1))

    assert events
    assert all(event.event_type == EventType.escalation_triggered for event in events)
