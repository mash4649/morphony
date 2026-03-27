from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from morphony.config import AgentConfig, load_config
from morphony.events import AuditLogReader, Event, EventType
from morphony.lifecycle import CheckpointManager, TaskLifecycleManager
from morphony.models import TaskState
from morphony.review import SelfEvaluationEngine


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _as_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return {}


def _as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return cast(list[object], value)
    return []


@dataclass(slots=True)
class TaskStatusReport:
    task_id: str
    state: TaskState
    goal: str | None
    elapsed_seconds: float
    completed_steps: int
    total_steps: int
    budget_remaining_ratio: float | None
    escalation_count: int
    improvement_count: int
    final_score: float | None
    summary_path: Path | None


@dataclass(slots=True)
class TaskSummaryReport:
    task_id: str
    summary_path: Path
    lines: list[str]


class ObservabilityEngine:
    def __init__(
        self,
        lifecycle_store: str | Path,
        checkpoint_dir: str | Path,
        audit_log: str | Path,
        memory_file: str | Path,
        summary_dir: str | Path,
        config_file: str | Path | None = None,
        config: AgentConfig | None = None,
    ) -> None:
        self.lifecycle_store = Path(lifecycle_store)
        self.checkpoint_dir = Path(checkpoint_dir)
        self.audit_log = Path(audit_log)
        self.memory_file = Path(memory_file)
        self.summary_dir = Path(summary_dir)
        self.config = config if config is not None else load_config(config_file)

    def build_status(self, task_id: str, goal: str | None = None) -> TaskStatusReport:
        lifecycle = TaskLifecycleManager(self.lifecycle_store)
        state = lifecycle.get_task_state(task_id)
        history = lifecycle.get_transition_history(task_id)
        elapsed_seconds = self._elapsed_seconds(history)
        checkpoint = CheckpointManager(self.checkpoint_dir).load_checkpoint(task_id)
        budget_remaining_ratio = self._budget_remaining_ratio(checkpoint)
        escalation_count = self._count_events(task_id, EventType.escalation_triggered)
        improvement_count = self._count_improvement_records(task_id)
        final_score = self._final_score(task_id)
        summary_path: Path | None = None
        if state == TaskState.completed:
            summary_path = self._write_summary(
                task_id=task_id,
                goal=goal,
                state=state,
                history=history,
                checkpoint=checkpoint,
                elapsed_seconds=elapsed_seconds,
                completed_steps=len(checkpoint.completed_steps) if checkpoint is not None else 0,
                total_steps=self._total_steps(checkpoint),
                budget_remaining_ratio=budget_remaining_ratio,
                escalation_count=escalation_count,
                improvement_count=improvement_count,
                final_score=final_score,
            ).summary_path
        return TaskStatusReport(
            task_id=task_id,
            state=state,
            goal=goal,
            elapsed_seconds=elapsed_seconds,
            completed_steps=len(checkpoint.completed_steps) if checkpoint is not None else 0,
            total_steps=self._total_steps(checkpoint),
            budget_remaining_ratio=budget_remaining_ratio,
            escalation_count=escalation_count,
            improvement_count=improvement_count,
            final_score=final_score,
            summary_path=summary_path,
        )

    def ensure_summary(self, task_id: str, goal: str | None = None) -> TaskSummaryReport:
        lifecycle = TaskLifecycleManager(self.lifecycle_store)
        state = lifecycle.get_task_state(task_id)
        if state != TaskState.completed:
            raise ValueError(f"Task '{task_id}' must be completed to generate a summary")

        history = lifecycle.get_transition_history(task_id)
        checkpoint = CheckpointManager(self.checkpoint_dir).load_checkpoint(task_id)
        elapsed_seconds = self._elapsed_seconds(history)
        budget_remaining_ratio = self._budget_remaining_ratio(checkpoint)
        escalation_count = self._count_events(task_id, EventType.escalation_triggered)
        improvement_count = self._count_improvement_records(task_id)
        final_score = self._final_score(task_id)
        completed_steps = len(checkpoint.completed_steps) if checkpoint is not None else 0
        total_steps = self._total_steps(checkpoint)
        return self._write_summary(
            task_id=task_id,
            goal=goal,
            state=state,
            history=history,
            checkpoint=checkpoint,
            elapsed_seconds=elapsed_seconds,
            completed_steps=completed_steps,
            total_steps=total_steps,
            budget_remaining_ratio=budget_remaining_ratio,
            escalation_count=escalation_count,
            improvement_count=improvement_count,
            final_score=final_score,
        )

    def watch_events(
        self,
        task_id: str,
        *,
        event_type: EventType | None = None,
        follow: bool = True,
        poll_interval_seconds: float = 0.5,
        timeout_seconds: float | None = None,
        goal: str | None = None,
    ) -> list[Event]:
        reader = AuditLogReader(self.audit_log)
        emitted: list[Event] = []
        seen = 0
        started = time.monotonic()

        while True:
            events = reader.iter_events(task_id=task_id, event_type=event_type)
            if seen < len(events):
                new_events = events[seen:]
                emitted.extend(new_events)
                seen = len(events)
                for event in new_events:
                    if event.event_type == EventType.state_changed:
                        to_state = _as_dict(event.payload).get("to_state")
                        if to_state == TaskState.completed.value:
                            self.ensure_summary(task_id, goal=goal)
            if not follow:
                break
            if timeout_seconds is not None and (time.monotonic() - started) >= timeout_seconds:
                break
            time.sleep(poll_interval_seconds)
        return emitted

    def _elapsed_seconds(self, history: list[object]) -> float:
        if not history:
            return 0.0
        first = history[0]
        timestamp = getattr(first, "timestamp", None)
        if not isinstance(timestamp, datetime):
            return 0.0
        elapsed = (_utc_now() - timestamp).total_seconds()
        return elapsed if elapsed > 0 else 0.0

    def _budget_remaining_ratio(self, checkpoint: object) -> float | None:
        if checkpoint is None:
            return None
        task_limit = self.config.budget.task.cost_usd
        if task_limit <= 0:
            return None
        used = float(getattr(checkpoint, "budget_delta", {}).get("cost_usd", 0.0))
        ratio = max(0.0, min(1.0, (task_limit - used) / task_limit))
        return round(ratio, 2)

    def _count_events(self, task_id: str, event_type: EventType) -> int:
        reader = AuditLogReader(self.audit_log)
        return len(reader.iter_events(task_id=task_id, event_type=event_type))

    def _count_improvement_records(self, task_id: str) -> int:
        if not self.memory_file.exists():
            return 0
        raw_text = self.memory_file.read_text(encoding="utf-8")
        if not raw_text.strip():
            return 0
        count = 0
        for line in raw_text.splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or payload.get("task_id") != task_id:
                continue
            episodic_memory = _as_dict(payload.get("episodic_memory"))
            metadata = _as_dict(episodic_memory.get("metadata"))
            if "improvement_loop" in metadata:
                count += 1
        return count

    def _final_score(self, task_id: str) -> float | None:
        report = SelfEvaluationEngine(self.memory_file).evaluate(task_id)
        if report is None:
            return None
        return report.overall_score

    def _latest_improvements(self, task_id: str) -> list[str]:
        report = SelfEvaluationEngine(self.memory_file).evaluate(task_id)
        if report is None:
            return []
        return [item for item in report.improvements if isinstance(item, str) and item.strip()]

    def _total_steps(self, checkpoint: object) -> int:
        if checkpoint is None:
            return 0
        step_records = getattr(checkpoint, "step_records", {})
        if isinstance(step_records, dict):
            return len(step_records)
        return 0

    def _summary_path(self, task_id: str) -> Path:
        return self.summary_dir / task_id / "summary.md"

    def _write_summary(
        self,
        *,
        task_id: str,
        goal: str | None,
        state: TaskState,
        history: list[object],
        checkpoint: object | None,
        elapsed_seconds: float,
        completed_steps: int,
        total_steps: int,
        budget_remaining_ratio: float | None,
        escalation_count: int,
        improvement_count: int,
        final_score: float | None,
    ) -> TaskSummaryReport:
        self_evaluation = SelfEvaluationEngine(self.memory_file).evaluate(task_id)
        improvement_lines = self._latest_improvements(task_id)
        summary_lines = [
            f"# Task Summary: {task_id}",
            "",
            f"- Goal: {goal or '(no goal)'}",
            f"- State: {state.value}",
            f"- Elapsed seconds: {elapsed_seconds:.1f}",
            f"- Transitions: {len(history)}",
            f"- Completed steps: {completed_steps}",
            f"- Total steps: {total_steps}",
            f"- Budget remaining ratio: {self._format_ratio(budget_remaining_ratio)}",
            f"- Escalations: {escalation_count}",
            f"- Improvement runs: {improvement_count}",
            f"- Final score: {self._format_score(final_score)}",
        ]
        if checkpoint is not None:
            last_completed_step_id = getattr(checkpoint, "last_completed_step_id", None)
            if last_completed_step_id is not None:
                summary_lines.append(f"- Last completed step: {last_completed_step_id}")
        if self_evaluation is not None:
            summary_lines.append("")
            summary_lines.append("## Self Evaluation")
            summary_lines.append(f"- Overall score: {self_evaluation.overall_score:.2f}")
            summary_lines.append(f"- Checklist: {self_evaluation.checklist_summary()}")
            summary_lines.append(f"- Fact Check: {self_evaluation.fact_check_summary()}")
            if self_evaluation.improvements:
                summary_lines.append("- Improvements:")
                for item in self_evaluation.improvements:
                    summary_lines.append(f"  - {item}")
        if improvement_lines:
            summary_lines.append("")
            summary_lines.append("## Lessons")
            for item in improvement_lines:
                summary_lines.append(f"- {item}")

        summary_path = self._summary_path(task_id)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
        return TaskSummaryReport(task_id=task_id, summary_path=summary_path, lines=summary_lines)

    def _format_ratio(self, value: float | None) -> str:
        if value is None:
            return "-"
        return f"{value:.2f}"

    def _format_score(self, value: float | None) -> str:
        if value is None:
            return "-"
        return f"{value:.2f}"
