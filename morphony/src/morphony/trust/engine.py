from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, cast

from morphony.models import EpisodicMemory, TaskState, TrustScore


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("trust timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp_text(value: datetime) -> str:
    return _to_utc(value).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return _to_utc(value)
    if isinstance(value, str) and value.strip():
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        return _to_utc(parsed)
    return datetime.now(UTC)


def _clamp_score(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return round(value, 2)


@dataclass(slots=True)
class _TrustObservation:
    task_id: str
    category: str
    success: bool
    owner_rating: float | None
    recorded_at: datetime


def _load_feedback_records(memory_file: Path) -> list[_TrustObservation]:
    if not memory_file.exists():
        return []
    raw_text = memory_file.read_text(encoding="utf-8")
    if not raw_text.strip():
        return []

    records: list[_TrustObservation] = []
    for line in raw_text.splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            continue
        typed_payload = cast(dict[str, object], payload)
        raw_memory = typed_payload.get("episodic_memory")
        if not isinstance(raw_memory, dict):
            continue
        memory_payload = dict(cast(dict[str, Any], raw_memory))
        raw_execution_state = memory_payload.get("execution_state")
        if isinstance(raw_execution_state, str):
            memory_payload["execution_state"] = TaskState(raw_execution_state)
        episodic_memory = EpisodicMemory.model_validate(memory_payload)
        category = episodic_memory.metadata.get("category")
        if not isinstance(category, str) or not category.strip():
            continue

        raw_rating = typed_payload.get("rating")
        owner_rating = float(raw_rating) if isinstance(raw_rating, (int, float)) else None
        raw_recorded_at = typed_payload.get("recorded_at")
        recorded_at = _parse_timestamp(raw_recorded_at)
        records.append(
            _TrustObservation(
                task_id=episodic_memory.task_id,
                category=category,
                success=episodic_memory.execution_state == TaskState.completed,
                owner_rating=owner_rating,
                recorded_at=recorded_at,
            )
        )
    return records


def _dedupe_latest_observations(
    observations: Iterable[_TrustObservation],
) -> dict[str, list[_TrustObservation]]:
    latest_by_category: dict[str, dict[str, _TrustObservation]] = defaultdict(dict)
    for observation in observations:
        category_map = latest_by_category[observation.category]
        previous = category_map.get(observation.task_id)
        if previous is None or observation.recorded_at >= previous.recorded_at:
            category_map[observation.task_id] = observation

    deduped: dict[str, list[_TrustObservation]] = {}
    for category, per_task in latest_by_category.items():
        deduped[category] = sorted(
            per_task.values(), key=lambda item: item.recorded_at, reverse=True
        )
    return deduped


class TrustScoreCalculator:
    def __init__(
        self,
        memory_file: str | Path,
        *,
        window_size: int = 20,
        rating_weight: float = 1.0,
        rating_scale_max: float = 5.0,
    ) -> None:
        self.memory_file = Path(memory_file)
        self.window_size = window_size
        self.rating_weight = rating_weight
        self.rating_scale_max = rating_scale_max

    def calculate(self) -> list[TrustScore]:
        if self.window_size <= 0:
            raise ValueError("window_size must be greater than zero")
        if not 0.0 <= self.rating_weight <= 1.0:
            raise ValueError("rating_weight must be between 0.0 and 1.0")
        if self.rating_scale_max <= 0.0:
            raise ValueError("rating_scale_max must be greater than zero")

        observations = _load_feedback_records(self.memory_file)
        grouped = _dedupe_latest_observations(observations)

        trust_scores: list[TrustScore] = []
        for category in sorted(grouped):
            selected = grouped[category][: self.window_size]
            if not selected:
                continue

            task_count = len(selected)
            success_count = sum(1 for observation in selected if observation.success)
            success_rate = success_count / task_count

            ratings = [
                observation.owner_rating
                for observation in selected
                if observation.owner_rating is not None
            ]
            avg_owner_rating = (
                round(sum(ratings) / len(ratings), 2) if ratings else None
            )
            if avg_owner_rating is None:
                score = success_rate
            else:
                normalized_rating = avg_owner_rating / self.rating_scale_max
                score = success_rate * (
                    (1.0 - self.rating_weight) + (self.rating_weight * normalized_rating)
                )

            trust_scores.append(
                TrustScore(
                    category=category,
                    score=_clamp_score(score),
                    task_count=task_count,
                    success_count=success_count,
                    avg_owner_rating=avg_owner_rating,
                    last_updated=max(item.recorded_at for item in selected),
                )
            )

        return trust_scores


class TrustScoreStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def replace_all(self, scores: list[TrustScore]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            self._ensure_schema(connection)
            connection.execute("DELETE FROM trust_scores")
            connection.executemany(
                """
                INSERT INTO trust_scores (
                    category,
                    score,
                    task_count,
                    success_count,
                    avg_owner_rating,
                    last_updated
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        score.category,
                        score.score,
                        score.task_count,
                        score.success_count,
                        score.avg_owner_rating,
                        _timestamp_text(score.last_updated),
                    )
                    for score in scores
                ],
            )
            connection.commit()

    def load_all(self) -> list[TrustScore]:
        if not self.path.exists():
            return []
        with sqlite3.connect(self.path) as connection:
            self._ensure_schema(connection)
            cursor = connection.execute(
                """
                SELECT category, score, task_count, success_count, avg_owner_rating, last_updated
                FROM trust_scores
                ORDER BY category ASC
                """
            )
            rows = cursor.fetchall()
        scores: list[TrustScore] = []
        for row in rows:
            category, score, task_count, success_count, avg_owner_rating, last_updated = row
            scores.append(
                TrustScore(
                    category=category,
                    score=float(score),
                    task_count=int(task_count),
                    success_count=int(success_count),
                    avg_owner_rating=None if avg_owner_rating is None else float(avg_owner_rating),
                    last_updated=_parse_timestamp(last_updated),
                )
            )
        return scores

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trust_scores (
                category TEXT PRIMARY KEY,
                score REAL NOT NULL,
                task_count INTEGER NOT NULL,
                success_count INTEGER NOT NULL,
                avg_owner_rating REAL,
                last_updated TEXT NOT NULL
            )
            """
        )
