from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from morphony.events import Event, EventBus, EventType
from morphony.models import SemanticMemory


CURRENT_SEMANTIC_MEMORY_STORE_VERSION = 1


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("semantic memory timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp_to_json(value: datetime) -> str:
    return _to_utc(value).isoformat().replace("+00:00", "Z")


def _timestamp_from_json(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    return _to_utc(parsed)


def _empty_record_map() -> dict[str, "SemanticMemoryRecord"]:
    return {}


def _metadata_dict(value: dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    return dict(value)


def _with_active_flag(memory: SemanticMemory, active: bool) -> SemanticMemory:
    metadata = _metadata_dict(memory.metadata)
    metadata["active"] = active
    return memory.model_copy(update={"metadata": metadata})


def _float_from_metadata(metadata: dict[str, Any], key: str) -> float:
    raw_value = metadata.get(key, 0.0)
    if isinstance(raw_value, (int, float)):
        return float(raw_value)
    return 0.0


def _source_episode_count(metadata: dict[str, Any]) -> int:
    raw_value = metadata.get("source_episodes", [])
    if isinstance(raw_value, list):
        return len(cast(list[Any], raw_value))
    return 0


def _is_active_flag(metadata: dict[str, Any]) -> bool:
    raw_value = metadata.get("active", True)
    if isinstance(raw_value, bool):
        return raw_value
    return True


def _migrate_semantic_memory_payload(memory_payload: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(memory_payload)
    raw_version = migrated.get("version")
    if not isinstance(raw_version, int) or raw_version < CURRENT_SEMANTIC_MEMORY_STORE_VERSION:
        migrated["version"] = CURRENT_SEMANTIC_MEMORY_STORE_VERSION

    raw_metadata = migrated.get("metadata")
    if not isinstance(raw_metadata, dict):
        raw_metadata = {}
        migrated["metadata"] = raw_metadata
    if "active" not in raw_metadata and "is_active" in raw_metadata:
        raw_is_active = raw_metadata.get("is_active")
        if isinstance(raw_is_active, bool):
            raw_metadata["active"] = raw_is_active
    return migrated


@dataclass(slots=True)
class SemanticMemoryRecord:
    memory: SemanticMemory
    created_at: datetime
    updated_at: datetime
    active: bool = True

    def to_data(self) -> dict[str, Any]:
        return {
            "memory": self.memory.model_dump(mode="json"),
            "created_at": _timestamp_to_json(self.created_at),
            "updated_at": _timestamp_to_json(self.updated_at),
            "active": self.active,
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> "SemanticMemoryRecord":
        raw_memory = data.get("memory")
        raw_created_at = data.get("created_at")
        raw_updated_at = data.get("updated_at")
        raw_active = data.get("active", True)
        if not isinstance(raw_memory, dict):
            raise ValueError("semantic memory record requires a memory object")
        if not isinstance(raw_created_at, str):
            raise ValueError("semantic memory record requires created_at string")
        if not isinstance(raw_updated_at, str):
            raise ValueError("semantic memory record requires updated_at string")
        if not isinstance(raw_active, bool):
            raise ValueError("semantic memory record active flag must be boolean")
        memory = SemanticMemory.model_validate(
            _migrate_semantic_memory_payload(cast(dict[str, Any], raw_memory))
        )
        return cls(
            memory=memory,
            created_at=_timestamp_from_json(raw_created_at),
            updated_at=_timestamp_from_json(raw_updated_at),
            active=raw_active,
        )


@dataclass(slots=True)
class SemanticMemoryStoreSnapshot:
    version: int = CURRENT_SEMANTIC_MEMORY_STORE_VERSION
    records: dict[str, SemanticMemoryRecord] = field(default_factory=_empty_record_map)

    def to_data(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "records": {
                pattern_id: record.to_data() for pattern_id, record in self.records.items()
            },
        }

    @classmethod
    def from_data(cls, data: dict[str, object]) -> "SemanticMemoryStoreSnapshot":
        raw_version = data.get("version", CURRENT_SEMANTIC_MEMORY_STORE_VERSION)
        if not isinstance(raw_version, int):
            raise ValueError("semantic memory store version must be an integer")
        if raw_version > CURRENT_SEMANTIC_MEMORY_STORE_VERSION:
            raise ValueError(f"unsupported semantic memory store version: {raw_version}")
        if raw_version < CURRENT_SEMANTIC_MEMORY_STORE_VERSION:
            raw_version = CURRENT_SEMANTIC_MEMORY_STORE_VERSION

        raw_records = data.get("records", {})
        if not isinstance(raw_records, dict):
            raise ValueError("semantic memory records must be an object")

        typed_records = cast(dict[object, object], raw_records)
        records: dict[str, SemanticMemoryRecord] = {}
        for raw_pattern_id, raw_record in typed_records.items():
            if not isinstance(raw_pattern_id, str):
                raise ValueError("semantic memory pattern ids must be strings")
            if not isinstance(raw_record, dict):
                raise ValueError("semantic memory records must be objects")
            records[raw_pattern_id] = SemanticMemoryRecord.from_data(
                cast(dict[str, object], raw_record)
            )
        return cls(version=raw_version, records=records)


class SemanticMemoryStore:
    def __init__(self, path: str | Path, event_bus: EventBus | None = None) -> None:
        self.path = Path(path)
        self.event_bus = event_bus

    def load(self) -> SemanticMemoryStoreSnapshot:
        if not self.path.exists():
            return SemanticMemoryStoreSnapshot()
        raw_text = self.path.read_text(encoding="utf-8")
        if not raw_text.strip():
            return SemanticMemoryStoreSnapshot()
        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid semantic memory store JSON at {self.path}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(
                f"Invalid semantic memory store root at {self.path}: expected object"
            )
        return SemanticMemoryStoreSnapshot.from_data(cast(dict[str, object], parsed))

    def save(self, snapshot: SemanticMemoryStoreSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        serialized = json.dumps(snapshot.to_data(), ensure_ascii=False, indent=2, sort_keys=True)
        temp_path.write_text(f"{serialized}\n", encoding="utf-8")
        temp_path.replace(self.path)

    def create(
        self,
        memory: SemanticMemory,
        *,
        created_at: datetime | None = None,
    ) -> SemanticMemoryRecord:
        snapshot = self.load()
        if memory.pattern_id in snapshot.records:
            raise ValueError(f"semantic memory already exists for pattern_id={memory.pattern_id}")
        timestamp = _to_utc(created_at or datetime.now(UTC))
        normalized_memory = _with_active_flag(memory, _is_active_flag(memory.metadata))
        record = SemanticMemoryRecord(
            memory=normalized_memory,
            created_at=timestamp,
            updated_at=timestamp,
            active=_is_active_flag(normalized_memory.metadata),
        )
        snapshot.records[normalized_memory.pattern_id] = record
        self._resolve_conflicts(snapshot, normalized_memory.category)
        self.save(snapshot)
        return snapshot.records[normalized_memory.pattern_id]

    def get(self, pattern_id: str) -> SemanticMemoryRecord:
        snapshot = self.load()
        record = snapshot.records.get(pattern_id)
        if record is None:
            raise KeyError(pattern_id)
        return record

    def read(self, pattern_id: str) -> SemanticMemory:
        return self.get(pattern_id).memory

    def update(
        self,
        task_or_memory: str | SemanticMemory,
        *,
        updated_at: datetime | None = None,
        **changes: Any,
    ) -> SemanticMemoryRecord:
        snapshot = self.load()
        if isinstance(task_or_memory, SemanticMemory):
            if changes:
                raise ValueError("update() does not accept field changes when memory is provided")
            memory = _with_active_flag(task_or_memory, _is_active_flag(task_or_memory.metadata))
            pattern_id = memory.pattern_id
            existing = snapshot.records.get(pattern_id)
            if existing is None:
                raise KeyError(pattern_id)
            old_category = existing.memory.category
        else:
            pattern_id = task_or_memory
            existing = snapshot.records.get(pattern_id)
            if existing is None:
                raise KeyError(pattern_id)
            old_category = existing.memory.category
            payload: dict[str, Any] = existing.memory.model_dump(mode="python")
            for field_name, field_value in changes.items():
                if field_name == "pattern_id" and field_value != pattern_id:
                    raise ValueError("semantic memory pattern_id cannot be changed")
                if field_name == "metadata" and isinstance(field_value, dict):
                    merged_metadata = _metadata_dict(payload["metadata"])
                    merged_metadata.update(cast(dict[str, Any], field_value))
                    payload["metadata"] = merged_metadata
                    continue
                payload[field_name] = field_value
            memory = _with_active_flag(
                SemanticMemory.model_validate(payload),
                _is_active_flag(cast(dict[str, Any], payload.get("metadata", {}))),
            )

        timestamp = _to_utc(updated_at or datetime.now(UTC))
        snapshot.records[memory.pattern_id] = SemanticMemoryRecord(
            memory=memory,
            created_at=existing.created_at,
            updated_at=timestamp,
            active=_is_active_flag(memory.metadata),
        )
        self._resolve_conflicts(snapshot, old_category)
        self._resolve_conflicts(snapshot, memory.category)
        self.save(snapshot)
        return snapshot.records[memory.pattern_id]

    def deactivate(self, pattern_id: str, *, updated_at: datetime | None = None) -> SemanticMemoryRecord:
        snapshot = self.load()
        existing = snapshot.records.get(pattern_id)
        if existing is None:
            raise KeyError(pattern_id)
        timestamp = _to_utc(updated_at or datetime.now(UTC))
        snapshot.records[pattern_id] = SemanticMemoryRecord(
            memory=_with_active_flag(existing.memory, False),
            created_at=existing.created_at,
            updated_at=timestamp,
            active=False,
        )
        self.save(snapshot)
        return snapshot.records[pattern_id]

    def delete(self, pattern_id: str) -> None:
        raise PermissionError("deletion is forbidden for semantic memory records")

    def list(self, *, active_only: bool = True) -> list[SemanticMemory]:
        return self.search(active_only=active_only)

    def search(
        self,
        *,
        category: str | None = None,
        active_only: bool = True,
    ) -> list[SemanticMemory]:
        snapshot = self.load()
        matches: list[SemanticMemoryRecord] = []
        for pattern_id in sorted(snapshot.records):
            record = snapshot.records[pattern_id]
            if active_only and not record.active:
                continue
            if category is not None and record.memory.category.casefold() != category.casefold():
                continue
            matches.append(record)
        matches.sort(key=_search_sort_key, reverse=True)
        return [record.memory for record in matches]

    def resolve_conflicts(self, category: str) -> list[SemanticMemoryRecord]:
        snapshot = self.load()
        winners = self._resolve_conflicts(snapshot, category)
        self.save(snapshot)
        return winners

    def _resolve_conflicts(
        self,
        snapshot: SemanticMemoryStoreSnapshot,
        category: str,
    ) -> list[SemanticMemoryRecord]:
        candidates = [
            snapshot.records[pattern_id]
            for pattern_id in sorted(snapshot.records)
            if snapshot.records[pattern_id].active
            and snapshot.records[pattern_id].memory.category.casefold() == category.casefold()
        ]
        if len(candidates) <= 1:
            return candidates

        winner = max(candidates, key=_conflict_rank_key)
        losers = [record for record in candidates if record.memory.pattern_id != winner.memory.pattern_id]
        for loser in losers:
            snapshot.records[loser.memory.pattern_id] = SemanticMemoryRecord(
                memory=_with_active_flag(loser.memory, False),
                created_at=loser.created_at,
                updated_at=loser.updated_at,
                active=False,
            )

        self._emit_conflict_event(
            winner=winner,
            losers=losers,
            category=category,
        )
        return [winner, *losers]

    def _emit_conflict_event(
        self,
        *,
        winner: SemanticMemoryRecord,
        losers: list[SemanticMemoryRecord],
        category: str,
    ) -> None:
        if self.event_bus is None:
            return
        event = Event(
            task_id=winner.memory.pattern_id,
            event_type=EventType.conflict_detected,
            payload={
                "category": category,
                "winner_pattern_id": winner.memory.pattern_id,
                "winner_source_episodes": _source_episode_count(winner.memory.metadata),
                "winner_confidence": _float_from_metadata(winner.memory.metadata, "confidence"),
                "winner_success_rate": winner.memory.success_rate,
                "loser_pattern_ids": [record.memory.pattern_id for record in losers],
            },
        )
        self.event_bus.publish_sync(event)


def _search_sort_key(record: SemanticMemoryRecord) -> tuple[float, float, str]:
    score = _float_from_metadata(record.memory.metadata, "confidence") + record.memory.success_rate
    return (score, record.updated_at.timestamp(), record.memory.pattern_id)


def _conflict_rank_key(record: SemanticMemoryRecord) -> tuple[int, float, float, str]:
    source_episode_count = _source_episode_count(record.memory.metadata)
    score = _float_from_metadata(record.memory.metadata, "confidence") + record.memory.success_rate
    return (
        source_episode_count,
        record.updated_at.timestamp(),
        score,
        record.memory.pattern_id,
    )
