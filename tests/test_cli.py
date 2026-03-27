from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from morphony.cli import app
from morphony.events import AuditLogReader, EventType
from morphony.lifecycle import CheckpointManager, TaskLifecycleManager
from morphony.models import TaskState


def _cli_runtime_paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    lifecycle_store = tmp_path / "runtime" / "lifecycle.json"
    checkpoint_dir = tmp_path / "runtime" / "checkpoints"
    task_registry = tmp_path / "runtime" / "task-registry.json"
    memory_file = tmp_path / "runtime" / "memory" / "episodic.jsonl"
    return lifecycle_store, checkpoint_dir, task_registry, memory_file


def _write_lifecycle_snapshot(path: Path, task_id: str, state: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "running_task_id": None,
                "pending_queue": [],
                "tasks": {
                    task_id: {
                        "state": state,
                        "history": [],
                    }
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _single_task_id(lifecycle_store: Path) -> str:
    task_states = TaskLifecycleManager(lifecycle_store).list_task_states()
    assert len(task_states) == 1, task_states
    return next(iter(task_states))


def test_cli_help_exits_zero() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0, result.output


def test_cli_rejects_safety_relaxation_override(tmp_path: Path) -> None:
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(
        """
escalation:
  l2_timeout_minutes: 15
  l2_timeout_policy: escalate
  l3_reminder_minutes: 30
  l3_auto_suspend_hours: 24
budget:
  task:
    cost_usd: 5
    time_minutes: 30
    api_calls: 100
  daily:
    cost_usd: 20
    time_hours: 4
  monthly:
    cost_usd: 200
improvement:
  max_iterations: 3
  trigger_threshold: 0.8
  completion_threshold: 0.9
memory:
  hot_episodes: 100
  semantic_max_per_category: 50
  inactive_threshold_days: 90
safety:
  sandbox_enabled: true
  kill_switch_enabled: true
""".strip()
        + "\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "config",
            "show",
            "--config-file",
            str(config_path),
            "--set",
            "safety.sandbox_enabled=false",
        ],
    )

    assert result.exit_code != 0
    assert "safety.sandbox_enabled" in result.output


def test_cli_resume_from_checkpoint(tmp_path: Path) -> None:
    task_id = "task-cli-resume"
    checkpoint_dir = tmp_path / "checkpoints"
    manager = CheckpointManager(checkpoint_dir)
    manager.save_step_completion(task_id, "step-1")

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "resume",
            task_id,
            "--checkpoint-dir",
            str(checkpoint_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "step-1" in result.output


def test_cli_run_status_and_gate_transitions(tmp_path: Path) -> None:
    lifecycle_store, checkpoint_dir, task_registry, memory_file = _cli_runtime_paths(tmp_path)
    runner = CliRunner()

    run_result = runner.invoke(
        app,
        [
            "run",
            "prepare release notes for the CLI issue",
            "--lifecycle-store",
            str(lifecycle_store),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--task-registry",
            str(task_registry),
            "--memory-file",
            str(memory_file),
        ],
    )

    assert run_result.exit_code == 0, run_result.output

    task_id = _single_task_id(lifecycle_store)
    assert TaskLifecycleManager(lifecycle_store).get_task_state(task_id) == TaskState.running

    status_result = runner.invoke(
        app,
        [
            "status",
            "--lifecycle-store",
            str(lifecycle_store),
            "--task-registry",
            str(task_registry),
        ],
    )

    assert status_result.exit_code == 0, status_result.output
    assert task_id in status_result.output
    assert TaskState.running.value in status_result.output

    detail_result = runner.invoke(
        app,
        [
            "status",
            task_id,
            "--lifecycle-store",
            str(lifecycle_store),
            "--task-registry",
            str(task_registry),
        ],
    )

    assert detail_result.exit_code == 0, detail_result.output
    assert task_id in detail_result.output
    assert TaskState.running.value in detail_result.output

    reject_result = runner.invoke(
        app,
        [
            "reject",
            task_id,
            "--reason",
            "needs another pass",
            "--lifecycle-store",
            str(lifecycle_store),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--task-registry",
            str(task_registry),
        ],
    )

    assert reject_result.exit_code == 0, reject_result.output
    rejected_state = TaskLifecycleManager(lifecycle_store).get_task_state(task_id)
    assert rejected_state in {TaskState.paused, TaskState.stopped}

    approve_result = runner.invoke(
        app,
        [
            "approve",
            task_id,
            "--lifecycle-store",
            str(lifecycle_store),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--task-registry",
            str(task_registry),
        ],
    )

    assert approve_result.exit_code == 0, approve_result.output
    assert TaskLifecycleManager(lifecycle_store).get_task_state(task_id) == TaskState.running


def test_cli_approve_then_commit_moves_planning_task_to_running(tmp_path: Path) -> None:
    lifecycle_store, checkpoint_dir, task_registry, _memory_file = _cli_runtime_paths(tmp_path)
    task_id = "task-cli-commit"
    _write_lifecycle_snapshot(lifecycle_store, task_id, TaskState.planning.value)

    runner = CliRunner()
    approve_result = runner.invoke(
        app,
        [
            "approve",
            task_id,
            "--lifecycle-store",
            str(lifecycle_store),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--task-registry",
            str(task_registry),
        ],
    )

    assert approve_result.exit_code == 0, approve_result.output
    assert TaskLifecycleManager(lifecycle_store).get_task_state(task_id) == TaskState.approved

    commit_result = runner.invoke(
        app,
        [
            "commit",
            task_id,
            "--lifecycle-store",
            str(lifecycle_store),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--task-registry",
            str(task_registry),
        ],
    )

    assert commit_result.exit_code == 0, commit_result.output
    assert "committed and moved to running" in commit_result.output
    assert TaskLifecycleManager(lifecycle_store).get_task_state(task_id) == TaskState.running


def test_cli_pause_saves_checkpoint_and_resume_restores_running(tmp_path: Path) -> None:
    lifecycle_store, checkpoint_dir, task_registry, memory_file = _cli_runtime_paths(tmp_path)
    runner = CliRunner()

    run_result = runner.invoke(
        app,
        [
            "run",
            "pause and resume a running task",
            "--lifecycle-store",
            str(lifecycle_store),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--task-registry",
            str(task_registry),
            "--memory-file",
            str(memory_file),
        ],
    )

    assert run_result.exit_code == 0, run_result.output

    task_id = _single_task_id(lifecycle_store)

    pause_result = runner.invoke(
        app,
        [
            "pause",
            task_id,
            "--lifecycle-store",
            str(lifecycle_store),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--task-registry",
            str(task_registry),
        ],
    )

    assert pause_result.exit_code == 0, pause_result.output
    checkpoint = CheckpointManager(checkpoint_dir).load_checkpoint(task_id)
    assert checkpoint is not None
    assert checkpoint.task_id == task_id
    assert TaskLifecycleManager(lifecycle_store).get_task_state(task_id) == TaskState.paused

    resume_result = runner.invoke(
        app,
        [
            "resume",
            task_id,
            "--lifecycle-store",
            str(lifecycle_store),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--task-registry",
            str(task_registry),
        ],
    )

    assert resume_result.exit_code == 0, resume_result.output
    assert task_id in resume_result.output
    assert TaskLifecycleManager(lifecycle_store).get_task_state(task_id) == TaskState.running


def test_cli_feedback_writes_episodic_memory_jsonl(tmp_path: Path) -> None:
    lifecycle_store, checkpoint_dir, task_registry, memory_file = _cli_runtime_paths(tmp_path)
    runner = CliRunner()

    run_result = runner.invoke(
        app,
        [
            "run",
            "record owner feedback for this task",
            "--lifecycle-store",
            str(lifecycle_store),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--task-registry",
            str(task_registry),
            "--memory-file",
            str(memory_file),
        ],
    )

    assert run_result.exit_code == 0, run_result.output

    task_id = _single_task_id(lifecycle_store)

    feedback_result = runner.invoke(
        app,
        [
            "feedback",
            task_id,
            "--rating",
            "5",
            "--comment",
            "clear and direct",
            "--lifecycle-store",
            str(lifecycle_store),
            "--task-registry",
            str(task_registry),
            "--memory-file",
            str(memory_file),
        ],
    )

    assert feedback_result.exit_code == 0, feedback_result.output
    assert memory_file.exists()

    records = [
        line
        for line in memory_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 1

    record = json.loads(records[0])
    assert record["task_id"] == task_id
    assert record["rating"] == 5
    assert record["comment"] == "clear and direct"


def test_cli_stop_stops_task_and_preserves_checkpoint_and_audit_log(tmp_path: Path) -> None:
    task_id = "task-cli-stop"
    lifecycle_store = tmp_path / "lifecycle.json"
    checkpoint_dir = tmp_path / "checkpoints"
    audit_log = tmp_path / "audit" / "audit.log"

    lifecycle = TaskLifecycleManager(lifecycle_store)
    lifecycle.submit_task(task_id)
    assert lifecycle.get_task_state(task_id) == TaskState.running

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "stop",
            task_id,
            "--lifecycle-store",
            str(lifecycle_store),
            "--checkpoint-dir",
            str(checkpoint_dir),
            "--audit-log",
            str(audit_log),
            "--artifact",
            "draft.md",
        ],
    )

    assert result.exit_code == 0, result.output
    reloaded = TaskLifecycleManager(lifecycle_store)
    assert reloaded.get_task_state(task_id) == TaskState.stopped

    checkpoint = CheckpointManager(checkpoint_dir).load_checkpoint(task_id)
    assert checkpoint is not None
    assert any(step.startswith("safety_stop_") for step in checkpoint.completed_steps)
    assert any("draft.md" in record.artifacts for record in checkpoint.step_records.values())

    events = AuditLogReader(audit_log).iter_events(task_id=task_id, event_type=EventType.state_changed)
    assert events
    assert any(
        event.payload.get("source") == "safety_controller"
        and event.payload.get("to_state") == TaskState.stopped.value
        for event in events
    )
