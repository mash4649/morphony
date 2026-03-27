from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from morphony.models import EpisodicMemory, TaskState


CURRENT_EPISODIC_MEMORY_STORE_VERSION = 1


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("memory timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp_to_json(value: datetime) -> str:
    return _to_utc(value).isoformat().replace("+00:00", "Z")


def _timestamp_from_json(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    return _to_utc(parsed)


def _empty_record_map() -> dict[str, "EpisodicMemoryRecord"]:
    return {}


def _migrate_memory_payload(memory_payload: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(memory_payload)
    raw_version = migrated.get("version")
    if not isinstance(raw_version, int) or raw_version < CURRENT_EPISODIC_MEMORY_STORE_VERSION:
        migrated["version"] = CURRENT_EPISODIC_MEMORY_STORE_VERSION

    if "execution_state" not in migrated:
        for legacy_key in ("state", "task_state", "status"):
            raw_state = migrated.get(legacy_key)
            if isinstance(raw_state, str) and raw_state:
                migrated["execution_state"] = TaskState(raw_state)
                migrated.pop(legacy_key, None)
                break
        else:
            migrated["execution_state"] = TaskState.pending
    else:
        raw_execution_state = migrated["execution_state"]
        if isinstance(raw_execution_state, str):
            migrated["execution_state"] = TaskState(raw_execution_state)
    for legacy_key in ("state", "task_state", "status"):
        migrated.pop(legacy_key, None)

    if "metadata" not in migrated or not isinstance(migrated["metadata"], dict):
        migrated["metadata"] = {}
    if "plan" not in migrated or not isinstance(migrated["plan"], list):
        migrated["plan"] = []
    if "steps" not in migrated or not isinstance(migrated["steps"], list):
        migrated["steps"] = []
    if "result" not in migrated:
        migrated["result"] = None
    return migrated


@dataclass(slots=True)
class EpisodicMemoryRecord:
    memory: EpisodicMemory
    created_at: datetime
    updated_at: datetime

    def to_data(self) -> dict[str, Any]:
        return {
            "memory": self.memory.model_dump(mode="json"),
            "created_at": _timestamp_to_json(self.created_at),
            "updated_at": _timestamp_to_json(self.updated_at),
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> "EpisodicMemoryRecord":
        raw_memory = data.get("memory")
        raw_created_at = data.get("created_at")
        raw_updated_at = data.get("updated_at")
        if not isinstance(raw_memory, dict):
            raise ValueError("episodic memory record requires a memory object")
        if not isinstance(raw_created_at, str):
            raise ValueError("episodic memory record requires created_at string")
        if not isinstance(raw_updated_at, str):
            raise ValueError("episodic memory record requires updated_at string")
        memory_payload = _migrate_memory_payload(cast(dict[str, Any], raw_memory))
        memory = EpisodicMemory.model_validate(memory_payload)
        return cls(
            memory=memory,
            created_at=_timestamp_from_json(raw_created_at),
            updated_at=_timestamp_from_json(raw_updated_at),
        )


@dataclass(slots=True)
class EpisodicMemoryStoreSnapshot:
    version: int = CURRENT_EPISODIC_MEMORY_STORE_VERSION
    records: dict[str, EpisodicMemoryRecord] = field(default_factory=_empty_record_map)

    def to_data(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "records": {
                task_id: record.to_data() for task_id, record in self.records.items()
            },
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> "EpisodicMemoryStoreSnapshot":
        raw_version = data.get("version", CURRENT_EPISODIC_MEMORY_STORE_VERSION)
        if not isinstance(raw_version, int):
            raise ValueError("episodic memory store version must be an integer")
        if raw_version > CURRENT_EPISODIC_MEMORY_STORE_VERSION:
            raise ValueError(
                "unsupported episodic memory store version: "
                f"{raw_version}"
            )
        if raw_version < CURRENT_EPISODIC_MEMORY_STORE_VERSION:
            raw_version = CURRENT_EPISODIC_MEMORY_STORE_VERSION

        raw_records = data.get("records", {})
        if not isinstance(raw_records, dict):
            raise ValueError("episodic memory records must be an object")

        typed_records = cast(dict[object, object], raw_records)
        records: dict[str, EpisodicMemoryRecord] = {}
        for raw_task_id, raw_record in typed_records.items():
            if not isinstance(raw_task_id, str):
                raise ValueError("episodic memory task ids must be strings")
            if not isinstance(raw_record, dict):
                raise ValueError("episodic memory records must be objects")
            records[raw_task_id] = EpisodicMemoryRecord.from_data(
                cast(dict[str, object], raw_record)
            )

        return cls(version=raw_version, records=records)


class EpisodicMemoryStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> EpisodicMemoryStoreSnapshot:
        if not self.path.exists():
            return EpisodicMemoryStoreSnapshot()
        raw_text = self.path.read_text(encoding="utf-8")
        if not raw_text.strip():
            return EpisodicMemoryStoreSnapshot()
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid episodic memory store JSON at {self.path}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(
                f"Invalid episodic memory store root at {self.path}: expected object"
            )
        return EpisodicMemoryStoreSnapshot.from_data(cast(dict[str, object], parsed))

    def save(self, snapshot: EpisodicMemoryStoreSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        serialized = json.dumps(snapshot.to_data(), ensure_ascii=False, indent=2, sort_keys=True)
        temp_path.write_text(f"{serialized}\n", encoding="utf-8")
        temp_path.replace(self.path)

    def create(
        self,
        memory: EpisodicMemory,
        *,
        created_at: datetime | None = None,
    ) -> EpisodicMemoryRecord:
        snapshot = self.load()
        if memory.task_id in snapshot.records:
            raise ValueError(f"episodic memory already exists for task_id={memory.task_id}")
        timestamp = _to_utc(created_at or datetime.now(UTC))
        record = EpisodicMemoryRecord(
            memory=memory,
            created_at=timestamp,
            updated_at=timestamp,
        )
        snapshot.records[memory.task_id] = record
        self.save(snapshot)
        return record

    def get(self, task_id: str) -> EpisodicMemoryRecord:
        snapshot = self.load()
        record = snapshot.records.get(task_id)
        if record is None:
            raise KeyError(task_id)
        return record

    def read(self, task_id: str) -> EpisodicMemory:
        return self.get(task_id).memory

    def update(
        self,
        task_or_memory: str | EpisodicMemory,
        *,
        updated_at: datetime | None = None,
        **changes: Any,
    ) -> EpisodicMemoryRecord:
        snapshot = self.load()
        if isinstance(task_or_memory, EpisodicMemory):
            if changes:
                raise ValueError("update() does not accept field changes when memory is provided")
            memory = task_or_memory
            task_id = memory.task_id
            existing = snapshot.records.get(task_id)
            if existing is None:
                raise KeyError(task_id)
        else:
            task_id = task_or_memory
            existing = snapshot.records.get(task_id)
            if existing is None:
                raise KeyError(task_id)
            payload: dict[str, Any] = existing.memory.model_dump(mode="python")
            for field_name, field_value in changes.items():
                if field_name == "task_id" and field_value != task_id:
                    raise ValueError("episodic memory task_id cannot be changed")
                payload[field_name] = field_value
            memory_payload: dict[str, Any] = payload.copy()
            raw_execution_state = memory_payload.get("execution_state")
            if isinstance(raw_execution_state, str):
                memory_payload["execution_state"] = TaskState(raw_execution_state)
            memory = EpisodicMemory.model_validate(memory_payload)
        timestamp = _to_utc(updated_at or datetime.now(UTC))
        record = EpisodicMemoryRecord(
            memory=memory,
            created_at=existing.created_at,
            updated_at=timestamp,
        )
        snapshot.records[memory.task_id] = record
        self.save(snapshot)
        return record

    def delete(self, task_id: str) -> None:
        raise PermissionError("deletion is forbidden for episodic memory records")

    def list(self) -> list[EpisodicMemory]:
        snapshot = self.load()
        return [
            snapshot.records[task_id].memory
            for task_id in sorted(snapshot.records)
        ]

    def search(
        self,
        *,
        task_id: str | None = None,
        goal_query: str | None = None,
        category: str | None = None,
        from_: datetime | None = None,
        to: datetime | None = None,
    ) -> list[EpisodicMemory]:
        snapshot = self.load()
        lower_bound = _to_utc(from_) if from_ is not None else None
        upper_bound = _to_utc(to) if to is not None else None

        matches: list[EpisodicMemory] = []
        for candidate_task_id in sorted(snapshot.records):
            record = snapshot.records[candidate_task_id]
            if task_id is not None and record.memory.task_id != task_id:
                continue
            if goal_query is not None and not _goal_matches(record.memory.goal, goal_query):
                continue
            if category is not None and not _category_matches(record.memory.metadata, category):
                continue
            if lower_bound is not None and (
                record.created_at < lower_bound and record.updated_at < lower_bound
            ):
                continue
            if upper_bound is not None and (
                record.created_at > upper_bound and record.updated_at > upper_bound
            ):
                continue
            matches.append(record.memory)
        return matches


def _goal_matches(goal: str, query: str) -> bool:
    normalized_goal = goal.casefold()
    normalized_query = query.casefold().strip()
    if not normalized_query:
        return True
    if normalized_query in normalized_goal:
        return True
    keywords = [part for part in re.split(r"\s+", normalized_query) if part]
    if not keywords:
        return True
    return any(keyword in normalized_goal for keyword in keywords)


def _category_matches(metadata: dict[str, Any], category: str) -> bool:
    raw_category = metadata.get("category")
    if not isinstance(raw_category, str):
        return False
    return raw_category.casefold() == category.casefold()
