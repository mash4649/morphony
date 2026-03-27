from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from morphony.models import TaskState


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("transition timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp_to_json(value: datetime) -> str:
    return _to_utc(value).isoformat().replace("+00:00", "Z")


def _timestamp_from_json(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    return _to_utc(parsed)


def _empty_transition_history() -> list["TransitionRecord"]:
    return []


def _empty_task_map() -> dict[str, "TaskLifecycleRecord"]:
    return {}


def _empty_queue() -> list[str]:
    return []


@dataclass(eq=True, slots=True)
class TransitionRecord:
    from_state: TaskState
    to_state: TaskState
    timestamp: datetime

    def to_data(self) -> dict[str, str]:
        return {
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "timestamp": _timestamp_to_json(self.timestamp),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> "TransitionRecord":
        from_state_raw = data.get("from_state")
        to_state_raw = data.get("to_state")
        timestamp_raw = data.get("timestamp")
        if not isinstance(from_state_raw, str) or not isinstance(to_state_raw, str):
            raise ValueError("transition record requires string from_state/to_state")
        if not isinstance(timestamp_raw, str):
            raise ValueError("transition record requires string timestamp")
        return cls(
            from_state=TaskState(from_state_raw),
            to_state=TaskState(to_state_raw),
            timestamp=_timestamp_from_json(timestamp_raw),
        )


@dataclass(slots=True)
class TaskLifecycleRecord:
    state: TaskState
    history: list[TransitionRecord] = field(default_factory=_empty_transition_history)

    def to_data(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "history": [entry.to_data() for entry in self.history],
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> "TaskLifecycleRecord":
        raw_state = data.get("state")
        if not isinstance(raw_state, str):
            raise ValueError("task lifecycle record requires state")
        raw_history = data.get("history", [])
        if not isinstance(raw_history, list):
            raise ValueError("task lifecycle record history must be a list")
        typed_history = cast(list[object], raw_history)
        history: list[TransitionRecord] = []
        for raw_item in typed_history:
            if not isinstance(raw_item, dict):
                raise ValueError("transition history entries must be objects")
            typed_raw_item = cast(dict[str, object], raw_item)
            history.append(TransitionRecord.from_data(typed_raw_item))
        return cls(
            state=TaskState(raw_state),
            history=history,
        )


@dataclass(slots=True)
class LifecycleSnapshot:
    version: int = 1
    tasks: dict[str, TaskLifecycleRecord] = field(default_factory=_empty_task_map)
    running_task_id: str | None = None
    pending_queue: list[str] = field(default_factory=_empty_queue)

    def to_data(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "running_task_id": self.running_task_id,
            "pending_queue": self.pending_queue,
            "tasks": {
                task_id: record.to_data() for task_id, record in self.tasks.items()
            },
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> "LifecycleSnapshot":
        raw_version = data.get("version", 1)
        if not isinstance(raw_version, int):
            raise ValueError("lifecycle snapshot version must be an integer")

        raw_running = data.get("running_task_id")
        if raw_running is not None and not isinstance(raw_running, str):
            raise ValueError("running_task_id must be a string or null")

        raw_queue = data.get("pending_queue", [])
        if not isinstance(raw_queue, list):
            raise ValueError("pending_queue must be a list")
        typed_queue = cast(list[object], raw_queue)
        pending_queue: list[str] = []
        for queue_item in typed_queue:
            if not isinstance(queue_item, str):
                raise ValueError("pending_queue entries must be strings")
            pending_queue.append(queue_item)

        raw_tasks = data.get("tasks", {})
        if not isinstance(raw_tasks, dict):
            raise ValueError("tasks must be an object")
        typed_tasks = cast(dict[object, object], raw_tasks)
        tasks: dict[str, TaskLifecycleRecord] = {}
        for raw_task_id, raw_record in typed_tasks.items():
            task_id = raw_task_id
            if not isinstance(task_id, str):
                raise ValueError("task ids must be strings")
            if not isinstance(raw_record, dict):
                raise ValueError("task records must be objects")
            typed_raw_record = cast(dict[str, object], raw_record)
            tasks[task_id] = TaskLifecycleRecord.from_data(typed_raw_record)

        return cls(
            version=raw_version,
            tasks=tasks,
            running_task_id=raw_running,
            pending_queue=pending_queue,
        )


class LifecycleStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> LifecycleSnapshot:
        if not self.path.exists():
            return LifecycleSnapshot()
        raw_text = self.path.read_text(encoding="utf-8")
        if not raw_text.strip():
            return LifecycleSnapshot()
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid lifecycle store JSON at {self.path}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"Invalid lifecycle store root at {self.path}: expected object")
        return LifecycleSnapshot.from_data(cast(dict[str, object], parsed))

    def save(self, snapshot: LifecycleSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        serialized = json.dumps(snapshot.to_data(), ensure_ascii=False, indent=2, sort_keys=True)
        temp_path.write_text(f"{serialized}\n", encoding="utf-8")
        temp_path.replace(self.path)
