from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from morphony.cli import app
from morphony.models import EpisodicMemory, TaskState
from morphony.review import ReviewEngine


def _write_feedback_record(path: Path, memory: EpisodicMemory) -> None:
    payload = {
        "task_id": memory.task_id,
        "rating": 5,
        "comment": "clear result with supporting evidence",
        "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "episodic_memory": memory.model_dump(mode="json"),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _reviewable_memory() -> EpisodicMemory:
    return EpisodicMemory(
        task_id="task-review-1",
        goal="review the deployment checklist",
        plan=["collect evidence", "summarize issues"],
        steps=[
            {"action": "collect evidence", "output": "found 2 logs"},
            {"action": "summarize issues", "output": "no blockers"},
        ],
        result={"status": "ready"},
        execution_state=TaskState.completed,
        metadata={
            "evidence": ["log-1", "log-2"],
            "total_cost": 3.5,
            "total_duration_minutes": 12,
        },
    )


def test_review_engine_approves_completed_memory_with_evidence(tmp_path: Path) -> None:
    memory_file = tmp_path / "episodic_feedback.jsonl"
    memory = _reviewable_memory()
    _write_feedback_record(memory_file, memory)

    report = ReviewEngine(memory_file).review(memory.task_id)

    assert report is not None
    assert report.verdict == "approved"
    assert report.overall_score >= 0.75
    assert report.goal_achievement >= 0.75
    assert report.checklist_summary().count("PASS") == 5


def test_review_cli_assess_reports_scores(tmp_path: Path) -> None:
    memory_file = tmp_path / "episodic_feedback.jsonl"
    memory = _reviewable_memory()
    _write_feedback_record(memory_file, memory)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "review",
            "assess",
            memory.task_id,
            "--memory-file",
            str(memory_file),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Review: task-review-1" in result.output
    assert "Verdict" in result.output
    assert "approved" in result.output
    assert "Goal Achievement" in result.output
