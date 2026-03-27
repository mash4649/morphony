from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from morphony.cli import app
from morphony.models import EpisodicMemory, TaskState
from morphony.trust import TrustScoreCalculator, TrustScoreStore


def _feedback_record(
    task_id: str,
    *,
    category: str,
    execution_state: TaskState,
    rating: float | None,
    recorded_at: datetime,
) -> dict[str, object]:
    memory = EpisodicMemory(
        task_id=task_id,
        goal=f"goal for {task_id}",
        execution_state=execution_state,
        metadata={"category": category},
    )
    payload: dict[str, object] = {
        "task_id": task_id,
        "comment": f"feedback for {task_id}",
        "recorded_at": recorded_at.isoformat().replace("+00:00", "Z"),
        "episodic_memory": memory.model_dump(mode="json"),
    }
    if rating is not None:
        payload["rating"] = rating
    return payload


def _write_feedback_file(path: Path, records: list[dict[str, object]]) -> Path:
    lines = [json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_trust_calculator_uses_recent_window_and_persists_to_sqlite(tmp_path: Path) -> None:
    feedback_file = tmp_path / "episodic_feedback.jsonl"
    db_file = tmp_path / "trust_scores.sqlite3"
    base = datetime(2026, 1, 1, tzinfo=UTC)
    records = [
        _feedback_record(
            f"task-{index:02d}",
            category="research",
            execution_state=TaskState.completed,
            rating=1 if index == 0 else 5,
            recorded_at=base + timedelta(minutes=index),
        )
        for index in range(21)
    ]
    _write_feedback_file(feedback_file, records)

    scores = TrustScoreCalculator(feedback_file, window_size=20).calculate()

    assert len(scores) == 1
    score = scores[0]
    assert score.category == "research"
    assert score.task_count == 20
    assert score.success_count == 20
    assert score.avg_owner_rating == 5.0
    assert score.score == 1.0

    store = TrustScoreStore(db_file)
    store.replace_all(scores)

    loaded = store.load_all()
    assert len(loaded) == 1
    assert loaded[0].category == "research"
    assert loaded[0].score == 1.0
    assert loaded[0].task_count == 20

    with sqlite3.connect(db_file) as connection:
        row = connection.execute(
            "SELECT category, score, task_count, success_count FROM trust_scores"
        ).fetchone()

    assert row == ("research", 1.0, 20, 20)


def test_trust_calculator_uses_success_rate_when_ratings_missing(tmp_path: Path) -> None:
    feedback_file = tmp_path / "episodic_feedback.jsonl"
    base = datetime(2026, 1, 1, tzinfo=UTC)
    records = [
        _feedback_record(
            f"task-{index:02d}",
            category="ops",
            execution_state=TaskState.completed if index < 2 else TaskState.failed,
            rating=None,
            recorded_at=base + timedelta(minutes=index),
        )
        for index in range(4)
    ]
    _write_feedback_file(feedback_file, records)

    scores = TrustScoreCalculator(feedback_file, window_size=20).calculate()

    assert len(scores) == 1
    score = scores[0]
    assert score.category == "ops"
    assert score.task_count == 4
    assert score.success_count == 2
    assert score.avg_owner_rating is None
    assert score.score == 0.5


def test_trust_cli_list_prints_scores_and_persists_sqlite(tmp_path: Path) -> None:
    feedback_file = tmp_path / "episodic_feedback.jsonl"
    db_file = tmp_path / "trust_scores.sqlite3"
    base = datetime(2026, 1, 1, tzinfo=UTC)
    records = [
        _feedback_record(
            "task-cli-1",
            category="research",
            execution_state=TaskState.completed,
            rating=5,
            recorded_at=base,
        ),
        _feedback_record(
            "task-cli-2",
            category="research",
            execution_state=TaskState.completed,
            rating=4,
            recorded_at=base + timedelta(minutes=1),
        ),
    ]
    _write_feedback_file(feedback_file, records)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "trust",
            "list",
            "--feedback-file",
            str(feedback_file),
            "--db-file",
            str(db_file),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Trust Scores" in result.output
    assert "research" in result.output
    assert "0.90" in result.output
    assert "4.50" in result.output
    assert db_file.exists()
