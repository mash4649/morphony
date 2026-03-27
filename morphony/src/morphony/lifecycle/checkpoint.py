from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Literal

from pydantic import Field, ValidationError, field_validator

from morphony.events import Event, EventBus, EventType
from morphony.models import TaskState
from morphony.models.memory import StrictModel

from .recovery import (
    FailureClass,
    RecoveryDecision,
    ResumeDecision,
    promote_transient_failure,
    transient_backoff_seconds,
)
from .state_machine import InvalidTransitionError, TERMINAL_STATES

logger = logging.getLogger(__name__)

CURRENT_CHECKPOINT_VERSION = 1

if TYPE_CHECKING:
    from .manager import TaskLifecycleManager


def _empty_str_list() -> list[str]:
    return []


def _empty_budget_delta() -> dict[str, float | int]:
    return {}


def _empty_step_record_map() -> dict[str, "CheckpointStepRecord"]:
    return {}


def _checkpoint_file_stem(task_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", task_id).strip("._-")
    if not slug:
        slug = "task"
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16]
    return f"{slug[:40]}-{digest}.checkpoint.json"


def _ensure_non_empty(name: str, value: str) -> str:
    if not value:
        raise ValueError(f"{name} must not be empty")
    return value


def _normalize_string_list(name: str, values: Sequence[object] | None) -> list[str]:
    if values is None:
        return []
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"{name} entries must be strings")
        result.append(value)
    return result


def _normalize_budget_delta(
    values: Mapping[str, object] | None,
) -> dict[str, float | int]:
    if values is None:
        return {}
    result: dict[str, float | int] = {}
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("budget_delta values must be int or float")
        result[key] = value
    return result


def _merge_unique(target: list[str], values: list[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _merge_budget_delta(
    target: dict[str, float | int],
    values: dict[str, float | int],
) -> None:
    for key, value in values.items():
        existing = target.get(key, 0)
        total = float(existing) + float(value)
        if float(total).is_integer():
            target[key] = int(total)
        else:
            target[key] = total


def _checkpoint_path(base_dir: Path, task_id: str) -> Path:
    return base_dir / _checkpoint_file_stem(task_id)


class CheckpointCorruptedError(Exception):
    pass


class CheckpointStepRecord(StrictModel):
    step_id: str
    status: Literal["completed", "skipped", "failed"] = "completed"
    artifacts: list[str] = Field(default_factory=_empty_str_list)
    budget_delta: dict[str, float | int] = Field(default_factory=_empty_budget_delta)
    failure_message: str | None = None
    failure_classification: FailureClass | None = None
    retry_count: int = Field(default=0, ge=0)

    @field_validator("failure_classification", mode="before")
    @classmethod
    def _normalize_failure_classification(cls, value: object) -> FailureClass | None:
        if value is None:
            return None
        if isinstance(value, FailureClass):
            return value
        if isinstance(value, str):
            return FailureClass(value)
        raise TypeError("failure_classification must be FailureClass, str, or null")


class CheckpointData(StrictModel):
    version: int = Field(default=CURRENT_CHECKPOINT_VERSION, ge=1)
    task_id: str
    completed_steps: list[str] = Field(default_factory=_empty_str_list)
    skipped_steps: list[str] = Field(default_factory=_empty_str_list)
    failed_steps: list[str] = Field(default_factory=_empty_str_list)
    step_records: dict[str, CheckpointStepRecord] = Field(default_factory=_empty_step_record_map)
    last_completed_step_id: str | None = None
    last_failed_step_id: str | None = None
    partial_artifacts: list[str] = Field(default_factory=_empty_str_list)
    budget_delta: dict[str, float | int] = Field(default_factory=_empty_budget_delta)


class CheckpointManager:
    def __init__(
        self,
        base_dir: str | Path,
        event_bus: EventBus | None = None,
        lifecycle_manager: "TaskLifecycleManager | None" = None,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._event_bus = event_bus
        self._lifecycle_manager = lifecycle_manager

    def save_step_completion(
        self,
        task_id: str,
        step_id: str,
        artifacts: list[str] | None = None,
        budget_delta: dict[str, float | int] | None = None,
    ) -> CheckpointData:
        task_id = _ensure_non_empty("task_id", task_id)
        step_id = _ensure_non_empty("step_id", step_id)
        normalized_artifacts = _normalize_string_list("artifacts", artifacts)
        normalized_budget_delta = _normalize_budget_delta(budget_delta)

        checkpoint = self._load_or_create_checkpoint(task_id)
        record = checkpoint.step_records.get(step_id)
        retry_count = record.retry_count if record is not None else 0
        checkpoint.step_records[step_id] = CheckpointStepRecord(
            step_id=step_id,
            status="completed",
            artifacts=normalized_artifacts,
            budget_delta=normalized_budget_delta,
            retry_count=retry_count,
        )

        if step_id not in checkpoint.completed_steps:
            checkpoint.completed_steps.append(step_id)
        if step_id in checkpoint.failed_steps:
            checkpoint.failed_steps.remove(step_id)
        if step_id in checkpoint.skipped_steps:
            checkpoint.skipped_steps.remove(step_id)
        checkpoint.last_completed_step_id = step_id
        _merge_budget_delta(checkpoint.budget_delta, normalized_budget_delta)

        self._save_checkpoint(checkpoint)
        self._publish_checkpoint_saved(
            checkpoint=checkpoint,
            step_id=step_id,
            artifacts=normalized_artifacts,
            budget_delta=normalized_budget_delta,
        )
        return checkpoint

    def handle_failure(
        self,
        task_id: str,
        step_id: str,
        classification: FailureClass,
        error_message: str,
        artifacts: list[str] | None = None,
    ) -> RecoveryDecision:
        task_id = _ensure_non_empty("task_id", task_id)
        step_id = _ensure_non_empty("step_id", step_id)
        normalized_artifacts = _normalize_string_list("artifacts", artifacts)

        checkpoint = self._load_or_create_checkpoint(task_id)
        previous_record = checkpoint.step_records.get(step_id)
        previous_retry_count = previous_record.retry_count if previous_record is not None else 0
        attempt = previous_retry_count + 1

        if classification is FailureClass.transient and promote_transient_failure(attempt):
            effective_classification = FailureClass.permanent
            promoted_to_permanent = True
        else:
            effective_classification = classification
            promoted_to_permanent = False

        if effective_classification is FailureClass.transient:
            retry_delay = transient_backoff_seconds(attempt)
            if retry_delay is None:
                raise RuntimeError("retry delay is unavailable for transient failure")
            checkpoint.step_records[step_id] = CheckpointStepRecord(
                step_id=step_id,
                status="failed",
                artifacts=normalized_artifacts,
                failure_message=error_message,
                failure_classification=FailureClass.transient,
                retry_count=attempt,
            )
            if step_id not in checkpoint.failed_steps:
                checkpoint.failed_steps.append(step_id)
            if step_id in checkpoint.completed_steps:
                checkpoint.completed_steps.remove(step_id)
            if step_id in checkpoint.skipped_steps:
                checkpoint.skipped_steps.remove(step_id)
            checkpoint.last_failed_step_id = step_id
            self._save_checkpoint(checkpoint)
            logger.warning(
                "Transient failure on task %s step %s (attempt %d): retry in %d seconds",
                task_id,
                step_id,
                attempt,
                retry_delay,
            )
            return RecoveryDecision(
                task_id=task_id,
                step_id=step_id,
                classification=FailureClass.transient,
                action="retry",
                checkpoint_path=str(self._checkpoint_path(task_id)),
                error_message=error_message,
                attempt=attempt,
                retry_delay_seconds=retry_delay,
                artifacts=normalized_artifacts,
            )

        if effective_classification is FailureClass.permanent:
            checkpoint.step_records[step_id] = CheckpointStepRecord(
                step_id=step_id,
                status="skipped",
                artifacts=normalized_artifacts,
                failure_message=error_message,
                failure_classification=effective_classification,
                retry_count=attempt,
            )
            if step_id not in checkpoint.skipped_steps:
                checkpoint.skipped_steps.append(step_id)
            if step_id in checkpoint.failed_steps:
                checkpoint.failed_steps.remove(step_id)
            if step_id in checkpoint.completed_steps:
                checkpoint.completed_steps.remove(step_id)
            checkpoint.last_failed_step_id = step_id
            self._save_checkpoint(checkpoint)
            if promoted_to_permanent:
                logger.warning(
                    "Transient failure promoted to permanent on task %s step %s after %d attempts",
                    task_id,
                    step_id,
                    attempt,
                )
            logger.warning(
                "Permanent failure on task %s step %s: skipping step and trying alternative",
                task_id,
                step_id,
            )
            return RecoveryDecision(
                task_id=task_id,
                step_id=step_id,
                classification=effective_classification,
                action="skip_step_and_try_alternative",
                checkpoint_path=str(self._checkpoint_path(task_id)),
                error_message=error_message,
                attempt=attempt,
                skip_step=True,
                alternative_trial=True,
                promoted_to_permanent=promoted_to_permanent,
                artifacts=normalized_artifacts,
            )

        checkpoint.step_records[step_id] = CheckpointStepRecord(
            step_id=step_id,
            status="failed",
            artifacts=normalized_artifacts,
            failure_message=error_message,
            failure_classification=FailureClass.fatal,
            retry_count=attempt,
        )
        if step_id not in checkpoint.failed_steps:
            checkpoint.failed_steps.append(step_id)
        if step_id in checkpoint.completed_steps:
            checkpoint.completed_steps.remove(step_id)
        if step_id in checkpoint.skipped_steps:
            checkpoint.skipped_steps.remove(step_id)
        checkpoint.last_failed_step_id = step_id
        _merge_unique(checkpoint.partial_artifacts, normalized_artifacts)
        self._save_checkpoint(checkpoint)
        logger.error(
            "Fatal failure on task %s step %s: preserving partial artifacts and escalating to L3",
            task_id,
            step_id,
        )

        if self._event_bus is not None:
            self._event_bus.publish_sync(
                Event(
                    task_id=task_id,
                    event_type=EventType.escalation_triggered,
                    payload={
                        "step_id": step_id,
                        "classification": FailureClass.fatal.value,
                        "error_message": error_message,
                        "artifacts": normalized_artifacts,
                        "checkpoint_path": str(self._checkpoint_path(task_id)),
                        "checkpoint_version": checkpoint.version,
                    },
                )
            )

        self._stop_task_if_possible(task_id)

        return RecoveryDecision(
            task_id=task_id,
            step_id=step_id,
            classification=FailureClass.fatal,
            action="escalate_l3_and_stop",
            checkpoint_path=str(self._checkpoint_path(task_id)),
            error_message=error_message,
            attempt=attempt,
            preserve_partial_artifacts=True,
            l3_escalation=True,
            artifacts=normalized_artifacts,
        )

    def resume_task(self, task_id: str) -> ResumeDecision | None:
        task_id = _ensure_non_empty("task_id", task_id)
        checkpoint = self.load_checkpoint(task_id)
        if checkpoint is None:
            logger.info("No checkpoint found for task %s; starting from the beginning", task_id)
            return None

        resume_after_step_id = checkpoint.last_completed_step_id
        if resume_after_step_id is None:
            logger.info("Resuming task %s from the beginning", task_id)
        else:
            logger.info(
                "Resuming task %s from checkpoint after step %s",
                task_id,
                resume_after_step_id,
            )

        return ResumeDecision(
            task_id=task_id,
            checkpoint_path=str(self._checkpoint_path(task_id)),
            checkpoint_version=checkpoint.version,
            resume_after_step_id=resume_after_step_id,
            completed_steps=list(checkpoint.completed_steps),
            skipped_steps=list(checkpoint.skipped_steps),
            partial_artifacts=list(checkpoint.partial_artifacts),
            budget_delta=dict(checkpoint.budget_delta),
            last_failed_step_id=checkpoint.last_failed_step_id,
        )

    def checkpoint_file_for_task(self, task_id: str) -> Path:
        task_id = _ensure_non_empty("task_id", task_id)
        return self._checkpoint_path(task_id)

    def load_checkpoint(self, task_id: str) -> CheckpointData | None:
        task_id = _ensure_non_empty("task_id", task_id)
        path = self._checkpoint_path(task_id)
        if not path.exists():
            return None

        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CheckpointCorruptedError(f"Checkpoint cannot be read at {path}: {exc}") from exc
        if not raw_text.strip():
            raise CheckpointCorruptedError(f"Checkpoint is empty at {path}")

        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise CheckpointCorruptedError(f"Checkpoint JSON is invalid at {path}: {exc}") from exc

        if not isinstance(payload, dict):
            raise CheckpointCorruptedError(f"Checkpoint root must be an object at {path}")

        try:
            checkpoint = CheckpointData.model_validate(payload)
        except ValidationError as exc:
            raise CheckpointCorruptedError(f"Checkpoint structure is invalid at {path}: {exc}") from exc

        if checkpoint.version != CURRENT_CHECKPOINT_VERSION:
            raise CheckpointCorruptedError(
                f"Unsupported checkpoint version {checkpoint.version} at {path}"
            )
        if checkpoint.task_id != task_id:
            raise CheckpointCorruptedError(
                f"Checkpoint task id mismatch at {path}: expected {task_id}, found {checkpoint.task_id}"
            )

        return checkpoint

    def _load_or_create_checkpoint(self, task_id: str) -> CheckpointData:
        checkpoint = self.load_checkpoint(task_id)
        if checkpoint is not None:
            return checkpoint
        return CheckpointData(task_id=task_id)

    def _save_checkpoint(self, checkpoint: CheckpointData) -> None:
        path = self._checkpoint_path(checkpoint.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.tmp")
        serialized = json.dumps(
            checkpoint.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        temp_path.write_text(f"{serialized}\n", encoding="utf-8")
        temp_path.replace(path)

    def _publish_checkpoint_saved(
        self,
        *,
        checkpoint: CheckpointData,
        step_id: str,
        artifacts: list[str],
        budget_delta: dict[str, float | int],
    ) -> None:
        if self._event_bus is None:
            return
        self._event_bus.publish_sync(
            Event(
                task_id=checkpoint.task_id,
                event_type=EventType.checkpoint_saved,
                payload={
                    "checkpoint_path": str(self._checkpoint_path(checkpoint.task_id)),
                    "checkpoint_version": checkpoint.version,
                    "step_id": step_id,
                    "artifacts": artifacts,
                    "budget_delta": budget_delta,
                    "completed_steps": list(checkpoint.completed_steps),
                },
            )
        )

    def _checkpoint_path(self, task_id: str) -> Path:
        return _checkpoint_path(self._base_dir, task_id)

    def _stop_task_if_possible(self, task_id: str) -> None:
        if self._lifecycle_manager is None:
            return
        try:
            state = self._lifecycle_manager.get_task_state(task_id)
        except KeyError:
            return

        if state in TERMINAL_STATES:
            return
        if state == TaskState.stopped:
            return

        try:
            self._lifecycle_manager.transition(task_id, TaskState.stopped)
        except (KeyError, InvalidTransitionError) as exc:
            logger.warning(
                "Failed to stop task %s after fatal failure: %s",
                task_id,
                exc,
            )


__all__ = [
    "CURRENT_CHECKPOINT_VERSION",
    "CheckpointCorruptedError",
    "CheckpointData",
    "CheckpointManager",
    "CheckpointStepRecord",
]
