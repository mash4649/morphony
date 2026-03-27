from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from morphony.cli import app
from morphony.config import AutonomyLevel, load_config
from morphony.lifecycle import TaskLifecycleManager
from morphony.memory import EpisodicMemoryStore, SemanticMemoryStore
from morphony.models import EpisodicMemory, SemanticMemory, TaskState


def _write_config(path: Path, *, autonomy_level: str) -> Path:
    payload = {
        "autonomy_level": autonomy_level,
        "escalation": {
            "l2_timeout_minutes": 15,
            "l2_timeout_policy": "escalate",
            "l3_reminder_minutes": 30,
            "l3_auto_suspend_hours": 24,
        },
        "budget": {
            "task": {"cost_usd": 5, "time_minutes": 30, "api_calls": 100},
            "daily": {"cost_usd": 20, "time_hours": 4},
            "monthly": {"cost_usd": 200},
        },
        "improvement": {
            "max_iterations": 3,
            "trigger_threshold": 0.8,
            "completion_threshold": 0.9,
        },
        "memory": {
            "hot_episodes": 3,
            "semantic_max_per_category": 50,
            "inactive_threshold_days": 90,
        },
        "safety": {
            "sandbox_enabled": True,
            "kill_switch_enabled": True,
        },
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _legacy_episode_payload(task_id: str, *, state: str = "completed") -> dict[str, object]:
    return {
        "version": 0,
        "task_id": task_id,
        "goal": f"legacy goal for {task_id}",
        "plan": ["collect", "validate"],
        "steps": [{"step": 1, "status": "done"}],
        "result": {"status": "ok"},
        "state": state,
        "metadata": {"category": "research"},
    }


def _current_episode_payload(task_id: str) -> dict[str, object]:
    return {
        "version": 1,
        "task_id": task_id,
        "goal": f"current goal for {task_id}",
        "plan": ["collect", "validate"],
        "steps": [{"step": 1, "status": "done"}],
        "result": {"status": "ok"},
        "execution_state": "completed",
        "metadata": {"category": "research"},
    }


def _serialize_batch(payload: object, *, suffix: str) -> str:
    if suffix == ".json":
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def test_cli_run_plan_only_keeps_task_pending(tmp_path: Path) -> None:
    runner = CliRunner()
    lifecycle_store = tmp_path / "runtime" / "lifecycle.json"
    checkpoint_dir = tmp_path / "runtime" / "checkpoints"
    task_registry = tmp_path / "runtime" / "tasks.json"
    memory_file = tmp_path / "runtime" / "memory" / "episodic_feedback.jsonl"
    config_file = _write_config(tmp_path / "agent_config.yaml", autonomy_level="plan_only")

    result = runner.invoke(
        app,
        [
            "run",
            "plan-only brownfield task",
            "--config-file",
            str(config_file),
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

    assert result.exit_code == 0, result.output
    assert "Autonomy level: plan_only" in result.output
    match = re.search(r"Created task '([^']+)'", result.output)
    assert match is not None, result.output
    task_id = match.group(1)

    config = load_config(config_file)
    assert config.autonomy_level == AutonomyLevel.plan_only

    lifecycle = TaskLifecycleManager(lifecycle_store)
    assert lifecycle.get_task_state(task_id) == TaskState.planning
    assert lifecycle.running_task_id is None
    assert lifecycle.pending_queue == []


@pytest.mark.parametrize("suffix", [".json", ".yaml"])
def test_memory_import_accepts_json_and_yaml_batches(
    tmp_path: Path,
    suffix: str,
) -> None:
    runner = CliRunner()
    source = tmp_path / f"episodic-import{suffix}"
    episodic_store = tmp_path / "runtime" / "memory" / "episodic.json"
    payload = [
        _current_episode_payload("task-current"),
        _legacy_episode_payload("task-legacy", state="running"),
    ]
    source.write_text(_serialize_batch(payload, suffix=suffix), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "memory",
            "import",
            str(source),
            "--episodic-store",
            str(episodic_store),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Imported 2 episodic memories." in result.output

    store = EpisodicMemoryStore(episodic_store)
    current = store.read("task-current")
    legacy = store.read("task-legacy")
    assert isinstance(current, EpisodicMemory)
    assert isinstance(legacy, EpisodicMemory)
    assert current.execution_state == TaskState.completed
    assert legacy.execution_state == TaskState.running
    assert legacy.version == 1


def test_legacy_memory_store_versions_are_migrated_append_only(tmp_path: Path) -> None:
    episodic_store_path = tmp_path / "episodic.json"
    semantic_store_path = tmp_path / "semantic.json"
    created_at = datetime(2026, 3, 27, 12, 0, tzinfo=UTC).isoformat().replace("+00:00", "Z")

    episodic_store_path.write_text(
        json.dumps(
            {
                "version": 0,
                "records": {
                    "task-legacy": {
                        "memory": _legacy_episode_payload("task-legacy"),
                        "created_at": created_at,
                        "updated_at": created_at,
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
    semantic_store_path.write_text(
        json.dumps(
            {
                "version": 0,
                "records": {
                    "pattern-legacy": {
                        "memory": {
                            "version": 0,
                            "pattern_id": "pattern-legacy",
                            "category": "research",
                            "pattern": "legacy pattern",
                            "conditions": ["legacy"],
                            "actions": ["migrate"],
                            "success_rate": 0.5,
                            "metadata": {"confidence": 0.75, "is_active": True},
                        },
                        "created_at": created_at,
                        "updated_at": created_at,
                        "active": True,
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

    episodic_snapshot = EpisodicMemoryStore(episodic_store_path).load()
    semantic_snapshot = SemanticMemoryStore(semantic_store_path).load()

    assert episodic_snapshot.version == 1
    assert semantic_snapshot.version == 1

    legacy_episode = EpisodicMemoryStore(episodic_store_path).read("task-legacy")
    legacy_pattern = SemanticMemoryStore(semantic_store_path).read("pattern-legacy")

    assert legacy_episode.execution_state == TaskState.completed
    assert legacy_episode.version == 1
    assert isinstance(legacy_pattern, SemanticMemory)
    assert legacy_pattern.metadata["active"] is True
