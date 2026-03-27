from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from typer.testing import CliRunner

from morphony.cli import app
from morphony.events import AuditLogWriter, Event, EventType


VALID_CONFIG_YAML = """
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
"""


def _write_agent_config(path: Path) -> Path:
    path.write_text(VALID_CONFIG_YAML.strip() + "\n", encoding="utf-8")
    return path


def _parse_json_output(output: str) -> object:
    start = output.find("{")
    end = output.rfind("}")
    assert start != -1, output
    assert end != -1, output
    return json.loads(output[start : end + 1])


def _as_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def test_cli_log_shows_task_audit_events_in_timestamp_order(tmp_path: Path) -> None:
    audit_log = tmp_path / "audit.log"
    writer = AuditLogWriter(audit_log)
    task_id = "task-cli-log"
    started_at = datetime(2026, 3, 26, 9, 0, tzinfo=UTC)

    writer.append(
        Event(
            task_id=task_id,
            event_type=EventType.task_completed,
            timestamp=started_at.replace(hour=9, minute=20),
            payload={"state": "completed"},
        )
    )
    writer.append(
        Event(
            task_id=task_id,
            event_type=EventType.task_started,
            timestamp=started_at,
            payload={"state": "running"},
        )
    )
    writer.append(
        Event(
            task_id=task_id,
            event_type=EventType.step_started,
            timestamp=started_at.replace(hour=9, minute=10),
            payload={"step": "plan"},
        )
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "log",
            task_id,
            "--audit-log",
            str(audit_log),
        ],
    )

    assert result.exit_code == 0, result.output
    assert task_id in result.output
    assert EventType.task_started.value in result.output
    assert EventType.step_started.value in result.output
    assert EventType.task_completed.value in result.output

    event_lines = [
        line
        for line in result.output.splitlines()
        if task_id in line
        and any(
            event_type.value in line
            for event_type in (
                EventType.task_started,
                EventType.step_started,
                EventType.task_completed,
            )
        )
    ]
    assert len(event_lines) == 3, result.output
    assert event_lines[0].find(EventType.task_started.value) != -1
    assert event_lines[1].find(EventType.step_started.value) != -1
    assert event_lines[2].find(EventType.task_completed.value) != -1


def test_cli_config_set_rejects_safety_relaxation(tmp_path: Path) -> None:
    config_path = _write_agent_config(tmp_path / "agent_config.yaml")

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "config",
            "set",
            "safety.sandbox_enabled",
            "false",
            "--config-file",
            str(config_path),
        ],
    )

    assert result.exit_code != 0, result.output
    assert "safety.sandbox_enabled" in result.output
    assert "sandbox_enabled: true" in config_path.read_text(encoding="utf-8")


def test_cli_config_set_updates_budget_value(tmp_path: Path) -> None:
    config_path = _write_agent_config(tmp_path / "agent_config.yaml")

    runner = CliRunner()
    set_result = runner.invoke(
        app,
        [
            "config",
            "set",
            "budget.task.cost_usd",
            "9",
            "--config-file",
            str(config_path),
        ],
    )

    assert set_result.exit_code == 0, set_result.output

    show_result = runner.invoke(
        app,
        [
            "config",
            "show",
            "--config-file",
            str(config_path),
        ],
    )

    assert show_result.exit_code == 0, show_result.output
    config = _as_dict(_parse_json_output(show_result.output))
    budget = _as_dict(config["budget"])
    task_budget = _as_dict(budget["task"])
    assert task_budget["cost_usd"] == 9


def test_cli_health_reports_multiple_tool_status_lines() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0, result.output

    status_lines = [
        line
        for line in result.output.splitlines()
        if "UP" in line or "DOWN" in line
    ]
    assert len(status_lines) >= 2, result.output
