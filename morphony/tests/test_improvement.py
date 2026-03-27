from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from morphony.cli import app
from morphony.improvement import ImprovementLoopEngine
from morphony.models import EpisodicMemory, TaskState


def _write_feedback_record(path: Path, memory: EpisodicMemory) -> None:
    payload = {
        "task_id": memory.task_id,
        "rating": 3,
        "comment": "needs another pass",
        "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "episodic_memory": memory.model_dump(mode="json"),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _low_score_memory(task_id: str) -> EpisodicMemory:
    return EpisodicMemory(
        task_id=task_id,
        goal="improve the draft",
        execution_state=TaskState.running,
        metadata={
            "total_cost": 8.0,
            "total_duration_minutes": 90.0,
        },
    )


def _degraded_memory(task_id: str) -> EpisodicMemory:
    return EpisodicMemory(
        task_id=task_id,
        goal="improve the draft",
        plan=["gather findings", "write summary"],
        steps=[{"action": "gather findings", "output": "reference set"}],
        result={"status": "draft"},
        execution_state=TaskState.completed,
        metadata={
            "artifacts": {
                "result_versions": ["result_v1.md", "result_v2.md"],
            },
            "feedback": {
                "rating": 5,
                "comment": "solid draft",
                "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            },
            "evidence": ["reference set"],
            "total_cost": 1.0,
            "total_duration_minutes": 10.0,
        },
    )


def test_improvement_loop_autorevises_and_records_memory(tmp_path: Path) -> None:
    memory_file = tmp_path / "episodic_feedback.jsonl"
    memory = _low_score_memory("task-improve-1")
    _write_feedback_record(memory_file, memory)

    report = ImprovementLoopEngine(memory_file).improve(memory.task_id)
    output_dir = tmp_path / "output" / memory.task_id

    assert report is not None
    assert report.triggered is True
    assert report.status == "completed"
    assert report.iterations >= 1
    assert report.final_score >= 0.9
    assert report.recorded_memory["metadata"]["improvement_loop"]["status"] == "completed"
    assert report.recorded_memory["result"] == str(output_dir / "result_final.md")
    assert (output_dir / "result_v1.md").exists()
    assert (output_dir / "result_final.md").exists()
    assert memory_file.exists()
    assert len([line for line in memory_file.read_text(encoding="utf-8").splitlines() if line.strip()]) == 2


def test_improvement_loop_detects_degradation_and_rolls_back(tmp_path: Path) -> None:
    memory_file = tmp_path / "episodic_feedback.jsonl"
    task_id = "task-improve-2"
    older_memory = _degraded_memory(task_id)
    current_memory = _low_score_memory(task_id)
    _write_feedback_record(memory_file, older_memory)
    with memory_file.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "task_id": task_id,
                    "rating": 1,
                    "comment": "regression",
                    "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "episodic_memory": current_memory.model_dump(mode="json"),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )

    report = ImprovementLoopEngine(memory_file).improve(task_id)
    output_dir = tmp_path / "output" / task_id

    assert report is not None
    assert report.status == "degraded"
    assert report.rollback_target == "result_v1.md"
    assert report.previous_score is not None
    assert report.final_score <= report.previous_score
    assert report.recorded_memory["result"] == str(output_dir / "result_final.md")
    assert (output_dir / "result_v1.md").exists()
    assert (output_dir / "result_final.md").exists()


def test_cli_review_improve_reports_json(tmp_path: Path) -> None:
    memory_file = tmp_path / "episodic_feedback.jsonl"
    memory = _low_score_memory("task-improve-cli")
    _write_feedback_record(memory_file, memory)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "review",
            "improve",
            memory.task_id,
            "--memory-file",
            str(memory_file),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Improvement Loop: task-improve-cli" in result.output
    assert "Status" in result.output
    assert '"status":"completed"' in result.output or '"status": "completed"' in result.output
    output_dir = tmp_path / "output" / memory.task_id
    assert (output_dir / "result_v1.md").exists()
    assert (output_dir / "result_final.md").exists()
