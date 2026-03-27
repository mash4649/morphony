from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from morphony.cli import app
from morphony.models import EpisodicMemory, TaskState
from morphony.review import SelfEvaluationEngine


def _write_feedback_record(path: Path, memory: EpisodicMemory) -> None:
    payload = {
        "task_id": memory.task_id,
        "rating": 3,
        "comment": "needs a fact check pass",
        "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "episodic_memory": memory.model_dump(mode="json"),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _self_evaluable_memory() -> EpisodicMemory:
    return EpisodicMemory(
        task_id="task-self-eval-1",
        goal="evaluate the release summary",
        plan=["gather findings", "write summary"],
        steps=[{"action": "gather findings", "output": "two references"}],
        result={"status": "draft"},
        execution_state=TaskState.completed,
        metadata={"total_cost": 2.5, "total_duration_minutes": 20},
    )


def test_self_evaluation_engine_returns_checklist_and_fact_checks(tmp_path: Path) -> None:
    memory_file = tmp_path / "episodic_feedback.jsonl"
    memory = _self_evaluable_memory()
    _write_feedback_record(memory_file, memory)

    report = SelfEvaluationEngine(memory_file).evaluate(memory.task_id)

    assert report is not None
    assert report.overall_score >= 0.0
    assert report.checklist_summary().count("PASS") >= 4
    assert "[要検証]" in report.fact_check_summary()
    payload = report.to_self_evaluation()
    assert "improvements" in payload
    assert "fact_checks" in payload


def test_self_evaluation_cli_evaluate_reports_json(tmp_path: Path) -> None:
    memory_file = tmp_path / "episodic_feedback.jsonl"
    memory = _self_evaluable_memory()
    _write_feedback_record(memory_file, memory)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "review",
            "evaluate",
            memory.task_id,
            "--memory-file",
            str(memory_file),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Self Evaluation: task-self-eval-1" in result.output
    assert "Fact Check" in result.output
    assert "[要検証]" in result.output
    assert '"improvements"' in result.output
