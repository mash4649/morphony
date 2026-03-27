from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from morphony.cli import app
from morphony.events import AuditLogWriter, Event, EventBus, EventType
from morphony.lifecycle import CheckpointManager, TaskLifecycleManager
from morphony.models import EpisodicMemory, TaskState


def _write_task_registry(path: Path, task_id: str, goal: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                task_id: {
                    "goal": goal,
                    "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                }
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_feedback_record(path: Path, memory: EpisodicMemory) -> None:
    payload = {
        "task_id": memory.task_id,
        "rating": 5,
        "comment": "clear and complete",
        "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "episodic_memory": memory.model_dump(mode="json"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _completed_memory(task_id: str, goal: str) -> EpisodicMemory:
    return EpisodicMemory(
        task_id=task_id,
        goal=goal,
        plan=["gather context", "write summary"],
        steps=[{"action": "gather context", "output": "facts collected"}],
        result={"summary": "done"},
        execution_state=TaskState.completed,
        metadata={
            "feedback": {
                "rating": 5,
                "comment": "clear and complete",
                "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            },
            "evidence": ["facts collected"],
            "total_cost": 1.0,
            "total_duration_minutes": 12.0,
        },
    )


def test_status_generates_summary_for_completed_task(tmp_path: Path) -> None:
    lifecycle_store = tmp_path / "runtime" / "lifecycle.json"
    checkpoint_dir = tmp_path / "runtime" / "checkpoints"
    task_registry = tmp_path / "runtime" / "tasks.json"
    memory_file = tmp_path / "runtime" / "memory" / "episodic_feedback.jsonl"
    audit_log = tmp_path / "runtime" / "audit" / "audit.log"
    summary_dir = tmp_path / "runtime" / "summaries"
    task_id = "task-observability-1"
    goal = "prepare release summary"

    _write_task_registry(task_registry, task_id, goal)
    memory = _completed_memory(task_id, goal)
    _write_feedback_record(memory_file, memory)

    bus = EventBus()
    writer = AuditLogWriter(audit_log)
    bus.subscribe_all(writer.append)
    lifecycle = TaskLifecycleManager(lifecycle_store, event_bus=bus)
    lifecycle.submit_task(task_id)
    CheckpointManager(checkpoint_dir).save_step_completion(task_id, "step-1", budget_delta={"cost_usd": 1.0})
    lifecycle.transition(task_id, TaskState.completed)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "status",
            task_id,
            "--lifecycle-store",
            str(lifecycle_store),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--task-registry",
            str(task_registry),
            "--audit-log",
            str(audit_log),
            "--memory-file",
            str(memory_file),
            "--summary-dir",
            str(summary_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    summary_path = summary_dir / task_id / "summary.md"
    assert summary_path.exists()
    assert "Summary:" in result.output
    assert "Budget remaining:" in result.output
    assert "# Task Summary: task-observability-1" in summary_path.read_text(encoding="utf-8")


def test_watch_filters_event_type_and_prints_state_changes(tmp_path: Path) -> None:
    lifecycle_store = tmp_path / "runtime" / "lifecycle.json"
    checkpoint_dir = tmp_path / "runtime" / "checkpoints"
    task_registry = tmp_path / "runtime" / "tasks.json"
    memory_file = tmp_path / "runtime" / "memory" / "episodic_feedback.jsonl"
    audit_log = tmp_path / "runtime" / "audit" / "audit.log"
    summary_dir = tmp_path / "runtime" / "summaries"
    task_id = "task-observability-2"
    goal = "monitor event stream"

    _write_task_registry(task_registry, task_id, goal)
    _write_feedback_record(memory_file, _completed_memory(task_id, goal))

    bus = EventBus()
    writer = AuditLogWriter(audit_log)
    bus.subscribe_all(writer.append)
    lifecycle = TaskLifecycleManager(lifecycle_store, event_bus=bus)
    lifecycle.submit_task(task_id)
    lifecycle.transition(task_id, TaskState.completed)
    writer.append(
        Event(
            task_id=task_id,
            event_type=EventType.error_occurred,
            payload={"message": "ignored by watch filter"},
        )
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "watch",
            task_id,
            "--once",
            "--event-type",
            "state_changed",
            "--lifecycle-store",
            str(lifecycle_store),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--task-registry",
            str(task_registry),
            "--audit-log",
            str(audit_log),
            "--memory-file",
            str(memory_file),
            "--summary-dir",
            str(summary_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "state_changed" in result.output
    assert "error_occurred" not in result.output
