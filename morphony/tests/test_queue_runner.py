from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from morphony.cli import app
from morphony.orchestration import QueueRunner


def _write_idle_queue_snapshot(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "running_task_id": None,
                "pending_queue": ["task-alpha", "task-beta"],
                "tasks": {
                    "task-alpha": {
                        "state": "pending",
                        "history": [],
                    },
                    "task-beta": {
                        "state": "pending",
                        "history": [],
                    },
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def test_queue_runner_starts_next_pending_task(tmp_path: Path) -> None:
    lifecycle_store = tmp_path / "lifecycle.json"
    _write_idle_queue_snapshot(lifecycle_store)

    result = QueueRunner(lifecycle_store).run_once()

    assert result.before_running_task_id is None
    assert result.before_pending_queue == ["task-alpha", "task-beta"]
    assert result.started_task_id == "task-alpha"
    assert result.after_running_task_id == "task-alpha"
    assert result.after_pending_queue == ["task-beta"]


def test_queue_run_cli_reports_started_task(tmp_path: Path) -> None:
    lifecycle_store = tmp_path / "lifecycle.json"
    _write_idle_queue_snapshot(lifecycle_store)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "queue",
            "run",
            "--lifecycle-store",
            str(lifecycle_store),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Started task 'task-alpha' from queue." in result.output
    assert "Running task: task-alpha" in result.output
    assert "Pending queue: task-beta" in result.output
