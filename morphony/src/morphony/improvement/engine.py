from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, cast

from morphony.config import AgentConfig, load_config
from morphony.models import EpisodicMemory, TaskState
from morphony.review import SelfEvaluationEngine


def _as_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return {}


def _as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return cast(list[object], value)
    return []


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class ImprovementLoopReport:
    task_id: str
    triggered: bool
    status: str
    iterations: int
    start_score: float
    final_score: float
    previous_score: float | None
    threshold: float
    completion_threshold: float
    lessons: list[str]
    rollback_target: str | None
    reasoning: str
    recorded_memory: dict[str, object]

    def to_improvement_loop(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "triggered": self.triggered,
            "status": self.status,
            "iterations": self.iterations,
            "start_score": self.start_score,
            "final_score": self.final_score,
            "previous_score": self.previous_score,
            "threshold": self.threshold,
            "completion_threshold": self.completion_threshold,
            "lessons": self.lessons,
            "rollback_target": self.rollback_target,
            "reasoning": self.reasoning,
            "recorded_memory": self.recorded_memory,
        }


class ImprovementLoopEngine:
    def __init__(
        self,
        memory_file: str | Path,
        config_file: str | Path | None = None,
        config: AgentConfig | None = None,
    ) -> None:
        self.memory_file = Path(memory_file)
        self._config = config if config is not None else load_config(config_file)
        self._evaluator = SelfEvaluationEngine(self.memory_file)

    def improve(
        self,
        task_id: str,
        *,
        artifact_dir: str | Path | None = None,
        revise_memory: Callable[[EpisodicMemory, list[str], int], EpisodicMemory] | None = None,
    ) -> ImprovementLoopReport | None:
        memories = self._load_task_memories(task_id)
        if not memories:
            return None

        latest_memory = memories[-1]
        latest_report = self._evaluator.evaluate_memory(latest_memory)
        threshold = self._config.improvement.trigger_threshold
        completion_threshold = self._config.improvement.completion_threshold
        max_iterations = self._config.improvement.max_iterations

        previous_memory = memories[-2] if len(memories) >= 2 else None
        previous_report = (
            self._evaluator.evaluate_memory(previous_memory) if previous_memory is not None else None
        )
        previous_score = previous_report.goal_achievement if previous_report is not None else None

        lessons = self._extract_lessons(latest_report)
        artifact_root = self._artifact_root(artifact_dir)
        artifact_task_dir = artifact_root / task_id
        artifact_version = 0
        status = "not_triggered"
        iterations = 0
        selected_memory = latest_memory
        selected_report = latest_report
        rollback_target: str | None = None
        reasoning_parts: list[str] = []

        if latest_report.goal_achievement < threshold:
            status = "running"
            reasoning_parts.append(
                f"triggered because goal_achievement={latest_report.goal_achievement:.2f} < {threshold:.2f}"
            )
            if previous_score is not None and latest_report.goal_achievement < previous_score:
                status = "degraded"
                rollback_target = self._resolve_rollback_target(
                    previous_memory,
                    artifact_dir=artifact_dir,
                )
                selected_memory = previous_memory if previous_memory is not None else latest_memory
                selected_report = (
                    previous_report if previous_report is not None else latest_report
                )
                selected_memory, artifact_version = self._save_result_artifact(
                    task_id=task_id,
                    memory=selected_memory,
                    report=selected_report,
                    version=artifact_version + 1,
                    artifact_task_dir=artifact_task_dir,
                    lessons=lessons,
                    status=status,
                    rollback_target=rollback_target,
                    update_final_alias=True,
                )
                reasoning_parts.append(
                    "latest score dropped below the previous score; rollback selected"
                )
            else:
                revision = revise_memory or self._auto_revise_memory
                selected_memory, artifact_version = self._save_result_artifact(
                    task_id=task_id,
                    memory=selected_memory,
                    report=selected_report,
                    version=artifact_version + 1,
                    artifact_task_dir=artifact_task_dir,
                    lessons=lessons,
                    status=status,
                    rollback_target=rollback_target,
                    update_final_alias=True,
                )
                for iteration in range(1, max_iterations + 1):
                    candidate = revision(selected_memory, lessons, iteration)
                    candidate_report = self._evaluator.evaluate_memory(candidate)
                    iterations = iteration
                    if candidate_report.goal_achievement < selected_report.goal_achievement:
                        status = "degraded"
                        rollback_target = self._resolve_rollback_target(
                            selected_memory,
                            artifact_dir=artifact_dir,
                        )
                        self._save_result_artifact(
                            task_id=task_id,
                            memory=candidate,
                            report=candidate_report,
                            version=artifact_version + 1,
                            artifact_task_dir=artifact_task_dir,
                            lessons=lessons,
                            status=status,
                            rollback_target=rollback_target,
                            update_final_alias=False,
                        )
                        reasoning_parts.append(
                            "revision lowered the score; rollback selected"
                        )
                        break
                    selected_memory = candidate
                    selected_report = candidate_report
                    artifact_version += 1
                    selected_memory, artifact_version = self._save_result_artifact(
                        task_id=task_id,
                        memory=selected_memory,
                        report=selected_report,
                        version=artifact_version,
                        artifact_task_dir=artifact_task_dir,
                        lessons=lessons,
                        status=status,
                        rollback_target=rollback_target,
                        update_final_alias=True,
                    )
                    reasoning_parts.append(
                        f"iteration {iteration} improved score to {candidate_report.goal_achievement:.2f}"
                    )
                    if candidate_report.goal_achievement >= completion_threshold:
                        status = "completed"
                        break
                else:
                    status = "max_iterations_reached"

                if status == "running":
                    status = (
                        "completed"
                        if selected_report.goal_achievement >= completion_threshold
                        else "max_iterations_reached"
                    )

        else:
            reasoning_parts.append(
                f"goal_achievement={latest_report.goal_achievement:.2f} reached trigger threshold"
            )
            selected_memory, artifact_version = self._save_result_artifact(
                task_id=task_id,
                memory=selected_memory,
                report=selected_report,
                version=artifact_version + 1,
                artifact_task_dir=artifact_task_dir,
                lessons=lessons,
                status=status,
                rollback_target=rollback_target,
                update_final_alias=True,
            )

        recorded_memory = self._record_memory(
            task_id=task_id,
            memory=selected_memory,
            status=status,
            iterations=iterations,
            start_score=latest_report.goal_achievement,
            final_score=selected_report.goal_achievement,
            previous_score=previous_score,
            lessons=lessons,
            rollback_target=rollback_target,
        )
        return ImprovementLoopReport(
            task_id=task_id,
            triggered=latest_report.goal_achievement < threshold,
            status=status,
            iterations=iterations,
            start_score=latest_report.goal_achievement,
            final_score=selected_report.goal_achievement,
            previous_score=previous_score,
            threshold=threshold,
            completion_threshold=completion_threshold,
            lessons=lessons,
            rollback_target=rollback_target,
            reasoning="; ".join(reasoning_parts) if reasoning_parts else "no improvement needed",
            recorded_memory=recorded_memory,
        )

    def _load_task_memories(self, task_id: str) -> list[EpisodicMemory]:
        if not self.memory_file.exists():
            return []
        raw_text = self.memory_file.read_text(encoding="utf-8")
        if not raw_text.strip():
            return []

        memories: list[EpisodicMemory] = []
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
            memories.append(EpisodicMemory.model_validate(memory_payload))
        return memories

    def _extract_lessons(self, report: object) -> list[str]:
        if not hasattr(report, "improvements"):
            return []
        improvements = cast(list[str], getattr(report, "improvements"))
        lessons = [lesson.strip() for lesson in improvements if isinstance(lesson, str) and lesson.strip()]
        if lessons:
            return lessons
        reasoning = getattr(report, "reasoning", "")
        if isinstance(reasoning, str) and reasoning.strip():
            return [reasoning.strip()]
        return ["review the latest self-evaluation"]

    def _auto_revise_memory(
        self,
        memory: EpisodicMemory,
        lessons: list[str],
        iteration: int,
    ) -> EpisodicMemory:
        metadata = _as_dict(memory.metadata).copy()
        feedback = _as_dict(metadata.get("feedback")).copy()
        feedback.setdefault("rating", 5)
        feedback.setdefault(
            "comment",
            "auto revision from improvement loop",
        )
        feedback.setdefault("recorded_at", _timestamp_text(_utc_now()))
        metadata["feedback"] = feedback
        metadata["evidence"] = lessons or ["auto_revision"]
        metadata["sources"] = ["self_evaluation"]
        metadata["total_cost"] = metadata.get("total_cost", 1.0)
        metadata["total_duration_minutes"] = metadata.get("total_duration_minutes", 5.0)
        metadata["improvement"] = {
            "iteration": iteration,
            "lessons": lessons,
            "revision_mode": "auto",
        }
        plan = list(memory.plan)
        if not plan:
            plan = [memory.goal or "improve the task"]
        for lesson in lessons:
            if lesson not in plan:
                plan.append(lesson)
        steps = list(memory.steps)
        steps.append(
            {
                "action": "apply improvement lessons",
                "iteration": iteration,
                "output": lessons,
            }
        )
        result = memory.result
        if isinstance(result, dict):
            revised_result: Any = result.copy()
            revised_result["status"] = "improved"
            revised_result["iteration"] = iteration
            revised_result["lessons"] = lessons
        else:
            revised_result = {
                "status": "improved",
                "iteration": iteration,
                "lessons": lessons,
            }
        return EpisodicMemory(
            task_id=memory.task_id,
            goal=memory.goal,
            plan=plan,
            steps=steps,
            result=revised_result,
            execution_state=TaskState.completed,
            metadata=metadata,
            version=memory.version,
        )

    def _artifact_root(self, artifact_dir: str | Path | None) -> Path:
        if artifact_dir is not None:
            return Path(artifact_dir)
        parent = self.memory_file.parent
        if parent.name == "memory" and parent.parent != parent:
            return parent.parent / "output"
        return parent / "output"

    def _save_result_artifact(
        self,
        *,
        task_id: str,
        memory: EpisodicMemory,
        report: object,
        version: int,
        artifact_task_dir: Path,
        lessons: list[str],
        status: str,
        rollback_target: str | None,
        update_final_alias: bool,
    ) -> tuple[EpisodicMemory, int]:
        artifact_task_dir.mkdir(parents=True, exist_ok=True)
        version_path = artifact_task_dir / f"result_v{version}.md"
        final_path = artifact_task_dir / "result_final.md"
        markdown = self._render_result_markdown(
            task_id=task_id,
            memory=memory,
            report=report,
            version=version,
            lessons=lessons,
            status=status,
            rollback_target=rollback_target,
        )
        version_path.write_text(markdown, encoding="utf-8")
        if update_final_alias:
            final_path.write_text(markdown, encoding="utf-8")

        updated_memory = memory.model_copy(deep=True)
        metadata = _as_dict(updated_memory.metadata).copy()
        artifacts = _as_dict(metadata.get("artifacts")).copy()
        version_paths = [
            str(item)
            for item in _as_list(artifacts.get("result_versions"))
            if isinstance(item, str) and item.strip()
        ]
        version_path_text = str(version_path)
        if version_path_text not in version_paths:
            version_paths.append(version_path_text)
        artifacts["result_versions"] = version_paths
        artifacts["result_final"] = str(final_path)
        artifacts["result_version"] = version
        metadata["artifacts"] = artifacts
        metadata["result_path"] = str(final_path)
        updated_memory.metadata = metadata
        updated_memory.result = str(final_path)
        return updated_memory, version

    def _render_result_markdown(
        self,
        *,
        task_id: str,
        memory: EpisodicMemory,
        report: object,
        version: int,
        lessons: list[str],
        status: str,
        rollback_target: str | None,
    ) -> str:
        lines = [
            f"# Result {version} for {task_id}",
            "",
            f"- Status: {status}",
            f"- Goal: {memory.goal}",
            f"- State: {memory.execution_state.value}",
            f"- Version: {version}",
        ]
        if rollback_target is not None:
            lines.append(f"- Rollback target: {rollback_target}")

        plan = [str(item) for item in memory.plan if isinstance(item, str) and item.strip()]
        if plan:
            lines.append("")
            lines.append("## Plan")
            for item in plan:
                lines.append(f"- {item}")

        steps = _as_list(memory.steps)
        if steps:
            lines.append("")
            lines.append("## Steps")
            for step in steps:
                lines.append(f"- {json.dumps(step, ensure_ascii=False, sort_keys=True)}")

        lines.append("")
        lines.append("## Result")
        lines.append(json.dumps(memory.result, ensure_ascii=False, indent=2, sort_keys=True))

        score = getattr(report, "overall_score", None)
        if isinstance(score, (int, float)):
            lines.append("")
            lines.append("## Evaluation")
            lines.append(f"- Overall score: {float(score):.2f}")

        if lessons:
            lines.append("")
            lines.append("## Lessons")
            for lesson in lessons:
                lines.append(f"- {lesson}")

        return "\n".join(lines) + "\n"

    def _resolve_rollback_target(
        self,
        memory: EpisodicMemory | None,
        *,
        artifact_dir: str | Path | None,
    ) -> str | None:
        if memory is None:
            return None
        metadata = _as_dict(memory.metadata)
        artifacts = _as_dict(metadata.get("artifacts"))
        versioned_results = _as_list(artifacts.get("result_versions"))
        if len(versioned_results) >= 2:
            previous = versioned_results[-2]
            if isinstance(previous, str) and previous.strip():
                return previous
        if artifact_dir is not None:
            version = metadata.get("result_version")
            if isinstance(version, int) and version > 1:
                return str(Path(artifact_dir) / f"result_v{version - 1}.md")
        result_path = metadata.get("result_path")
        if isinstance(result_path, str) and result_path.strip():
            return result_path
        return None

    def _record_memory(
        self,
        *,
        task_id: str,
        memory: EpisodicMemory,
        status: str,
        iterations: int,
        start_score: float,
        final_score: float,
        previous_score: float | None,
        lessons: list[str],
        rollback_target: str | None,
    ) -> dict[str, object]:
        recorded_memory = memory.model_copy(deep=True)
        metadata = _as_dict(recorded_memory.metadata).copy()
        metadata["improvement_loop"] = {
            "task_id": task_id,
            "status": status,
            "iterations": iterations,
            "start_score": start_score,
            "final_score": final_score,
            "previous_score": previous_score,
            "lessons": lessons,
            "rollback_target": rollback_target,
            "recorded_at": _timestamp_text(_utc_now()),
        }
        recorded_memory.metadata = metadata

        payload = {
            "task_id": task_id,
            "recorded_at": _timestamp_text(_utc_now()),
            "episodic_memory": recorded_memory.model_dump(mode="json"),
            "improvement_loop": metadata["improvement_loop"],
        }
        self.memory_file.parent.mkdir(parents=True, exist_ok=True)
        with self.memory_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
        return recorded_memory.model_dump(mode="json")
