from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from morphony.models import EpisodicMemory, TaskState


def _bounded_score(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return round(value, 2)


def _as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return cast(list[object], value)
    return []


def _as_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return {}


def _text_length(value: object) -> int:
    if isinstance(value, str):
        return len(value.strip())
    return 0


@dataclass(slots=True)
class ReviewReport:
    task_id: str
    verdict: str
    goal_achievement: float
    information_reliability: float
    structure_clarity: float
    efficiency: float
    reasoning: str
    checklist: list[tuple[str, bool, str]]

    @property
    def overall_score(self) -> float:
        return round(
            (
                self.goal_achievement
                + self.information_reliability
                + self.structure_clarity
                + self.efficiency
            )
            / 4.0,
            2,
        )

    def checklist_summary(self) -> str:
        return "; ".join(
            f"{name}={'PASS' if passed else 'FAIL'}" for name, passed, _ in self.checklist
        )


@dataclass(slots=True)
class SelfEvaluationReport:
    task_id: str
    goal_achievement: float
    information_reliability: float
    structure_clarity: float
    efficiency: float
    reasoning: str
    checklist: list[tuple[str, bool, str]]
    fact_checks: list[tuple[str, bool, str]]
    improvements: list[str]

    @property
    def overall_score(self) -> float:
        return round(
            (
                self.goal_achievement
                + self.information_reliability
                + self.structure_clarity
                + self.efficiency
            )
            / 4.0,
            2,
        )

    def checklist_summary(self) -> str:
        return "; ".join(
            f"{name}={'PASS' if passed else 'FAIL'}" for name, passed, _ in self.checklist
        )

    def fact_check_summary(self) -> str:
        return "; ".join(
            f"{name}={'OK' if passed else '[要検証]'}" for name, passed, _ in self.fact_checks
        )

    def to_self_evaluation(self) -> dict[str, object]:
        return {
            "goal_achievement": self.goal_achievement,
            "information_reliability": self.information_reliability,
            "structure_clarity": self.structure_clarity,
            "efficiency": self.efficiency,
            "overall_score": self.overall_score,
            "reasoning": self.reasoning,
            "improvements": self.improvements,
            "checklist": [
                {"name": name, "passed": passed, "reason": reason}
                for name, passed, reason in self.checklist
            ],
            "fact_checks": [
                {"name": name, "passed": passed, "reason": reason}
                for name, passed, reason in self.fact_checks
            ],
        }


def _score_memory(memory: EpisodicMemory) -> dict[str, object]:
    goal = memory.goal.strip()
    plan = memory.plan
    steps = memory.steps
    metadata = memory.metadata
    result = memory.result
    feedback = _as_dict(metadata.get("feedback"))
    evidence = _as_list(metadata.get("evidence"))
    sources = _as_list(metadata.get("sources"))
    evidence_count = len(evidence) + len(sources)
    feedback_comment = feedback.get("comment")
    feedback_rating = feedback.get("rating")
    rating_value = feedback_rating if isinstance(feedback_rating, (int, float)) else None

    goal_achievement = _bounded_score(
        0.25
        + (0.35 if memory.execution_state == TaskState.completed else 0.0)
        + (0.20 if result is not None else 0.0)
        + (0.20 if steps else 0.0)
    )
    information_reliability = _bounded_score(
        0.30
        + (0.30 if evidence_count > 0 else 0.0)
        + (0.10 if result is not None else 0.0)
        + (0.10 if isinstance(rating_value, (int, float)) and rating_value >= 4 else 0.0)
        + (0.05 if isinstance(feedback_comment, str) and feedback_comment.strip() else 0.0)
    )
    structure_clarity = _bounded_score(
        0.20
        + (0.20 if goal else 0.0)
        + (0.20 if plan else 0.0)
        + (0.20 if steps else 0.0)
        + (0.20 if result is not None else 0.0)
    )
    efficiency = _bounded_score(
        0.45
        + (0.20 if 0 < len(steps) <= 3 else 0.10 if len(steps) <= 5 else 0.0)
        + (0.10 if evidence_count > 0 else 0.0)
        + (
            0.10
            if isinstance(metadata.get("total_cost"), (int, float)) and float(metadata["total_cost"]) <= 5.0
            else 0.0
        )
        + (
            0.05
            if isinstance(metadata.get("total_duration_minutes"), (int, float))
            and float(metadata["total_duration_minutes"]) <= 60.0
            else 0.0
        )
    )

    checklist = [
        ("goal_documented", bool(goal), "goal text is present" if goal else "goal text is missing"),
        (
            "result_documented",
            result is not None,
            "result is recorded" if result is not None else "result is missing",
        ),
        (
            "plan_documented",
            bool(plan),
            "plan entries are recorded" if plan else "plan entries are missing",
        ),
        (
            "steps_documented",
            bool(steps),
            "execution steps are recorded" if steps else "execution steps are missing",
        ),
        (
            "evidence_documented",
            evidence_count > 0 or isinstance(feedback_comment, str) and feedback_comment.strip() != "",
            "supporting evidence or feedback is present"
            if evidence_count > 0 or isinstance(feedback_comment, str) and feedback_comment.strip() != ""
            else "supporting evidence or feedback is missing",
        ),
    ]

    fact_checks = [
        (
            "result_has_payload",
            result is not None,
            "result payload verified" if result is not None else "[要検証] result payload missing",
        ),
        (
            "evidence_has_sources",
            evidence_count > 0,
            "evidence sources verified" if evidence_count > 0 else "[要検証] no evidence or sources",
        ),
        (
            "feedback_has_comment",
            isinstance(feedback_comment, str) and feedback_comment.strip() != "",
            "feedback comment verified"
            if isinstance(feedback_comment, str) and feedback_comment.strip() != ""
            else "[要検証] feedback comment missing",
        ),
    ]

    passed_count = sum(1 for _, passed, _ in checklist if passed)
    evidence_count = len(evidence) + len(sources)
    improvements: list[str] = []
    for name, passed, reason in checklist:
        if not passed:
            improvements.append(f"{name}: {reason}")
    for name, passed, reason in fact_checks:
        if not passed:
            improvements.append(f"{name}: {reason}")

    reasoning_parts = []
    if memory.execution_state != TaskState.completed:
        reasoning_parts.append(f"task is {memory.execution_state.value}")
    if evidence_count == 0:
        reasoning_parts.append("no supporting evidence recorded")
    if rating_value is not None:
        reasoning_parts.append(f"feedback rating={rating_value:g}")
    reasoning_parts.append(f"{passed_count}/5 checklist items passed")
    if not reasoning_parts:
        reasoning_parts.append("self evaluation passed all checks")

    return {
        "goal_achievement": goal_achievement,
        "information_reliability": information_reliability,
        "structure_clarity": structure_clarity,
        "efficiency": efficiency,
        "checklist": checklist,
        "fact_checks": fact_checks,
        "reasoning": ", ".join(reasoning_parts),
        "improvements": improvements,
    }


class ReviewEngine:
    def __init__(self, memory_file: str | Path) -> None:
        self.memory_file = Path(memory_file)

    def review(self, task_id: str) -> ReviewReport | None:
        memory = self._load_latest_memory(task_id)
        if memory is None:
            return None
        return self.review_memory(memory)

    def review_memory(self, memory: EpisodicMemory) -> ReviewReport:
        scored = _score_memory(memory)
        checklist = cast(list[tuple[str, bool, str]], scored["checklist"])
        passed_count = sum(1 for _, passed, _ in checklist if passed)
        goal_achievement = cast(float, scored["goal_achievement"])
        information_reliability = cast(float, scored["information_reliability"])
        structure_clarity = cast(float, scored["structure_clarity"])
        efficiency = cast(float, scored["efficiency"])
        verdict = (
            "approved"
            if memory.execution_state == TaskState.completed
            and passed_count == len(checklist)
            and ((goal_achievement + information_reliability + structure_clarity + efficiency) / 4.0) >= 0.75
            else "needs_revision"
        )
        reasoning = cast(str, scored["reasoning"])
        return ReviewReport(
            task_id=memory.task_id,
            verdict=verdict,
            goal_achievement=goal_achievement,
            information_reliability=information_reliability,
            structure_clarity=structure_clarity,
            efficiency=efficiency,
            reasoning=reasoning,
            checklist=checklist,
        )

    def _load_latest_memory(self, task_id: str) -> EpisodicMemory | None:
        if not self.memory_file.exists():
            return None
        latest: EpisodicMemory | None = None
        raw_text = self.memory_file.read_text(encoding="utf-8")
        if not raw_text.strip():
            return None
        for line in raw_text.splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("task_id") != task_id:
                continue
            raw_memory = payload.get("episodic_memory")
            if not isinstance(raw_memory, dict):
                continue
            memory_payload: dict[str, Any] = cast(dict[str, Any], raw_memory).copy()
            raw_execution_state = memory_payload.get("execution_state")
            if isinstance(raw_execution_state, str):
                memory_payload["execution_state"] = TaskState(raw_execution_state)
            latest = EpisodicMemory.model_validate(memory_payload)
        return latest


class SelfEvaluationEngine:
    def __init__(self, memory_file: str | Path) -> None:
        self.memory_file = Path(memory_file)

    def evaluate(self, task_id: str) -> SelfEvaluationReport | None:
        memory = ReviewEngine(self.memory_file)._load_latest_memory(task_id)
        if memory is None:
            return None
        return self.evaluate_memory(memory)

    def evaluate_memory(self, memory: EpisodicMemory) -> SelfEvaluationReport:
        scored = _score_memory(memory)
        checklist = cast(list[tuple[str, bool, str]], scored["checklist"])
        fact_checks = cast(list[tuple[str, bool, str]], scored["fact_checks"])
        improvements = cast(list[str], scored["improvements"])
        return SelfEvaluationReport(
            task_id=memory.task_id,
            goal_achievement=cast(float, scored["goal_achievement"]),
            information_reliability=cast(float, scored["information_reliability"]),
            structure_clarity=cast(float, scored["structure_clarity"]),
            efficiency=cast(float, scored["efficiency"]),
            reasoning=cast(str, scored["reasoning"]),
            checklist=checklist,
            fact_checks=fact_checks,
            improvements=improvements,
        )
