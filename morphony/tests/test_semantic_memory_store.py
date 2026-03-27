from __future__ import annotations

import importlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import pytest

from morphony.events import EventBus, EventType
from morphony.models import SemanticMemory


class _SemanticMemoryStore(Protocol):
    def create(self, memory: SemanticMemory) -> object: ...

    def get(self, pattern_id: str) -> object: ...

    def read(self, pattern_id: str) -> object: ...

    def update(self, pattern_id: str, **changes: Any) -> object: ...

    def deactivate(self, pattern_id: str) -> object: ...

    def search(
        self,
        *,
        category: str | None = None,
        active_only: bool = True,
    ) -> list[SemanticMemory]: ...

    def delete(self, pattern_id: str) -> object: ...


class _SemanticMemoryStoreFactory(Protocol):
    def __call__(
        self,
        path: str | Path,
        event_bus: EventBus | None = None,
    ) -> _SemanticMemoryStore: ...


def _load_store_factory() -> _SemanticMemoryStoreFactory:
    for module_name in ("morphony.memory.semantic_store", "morphony.memory_store", "morphony.memory"):
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        store_cls = getattr(module, "SemanticMemoryStore", None)
        if store_cls is not None:
            return cast(_SemanticMemoryStoreFactory, store_cls)
    raise ModuleNotFoundError("SemanticMemoryStore is not available yet")


def _make_store(tmp_path: Path, event_bus: EventBus | None = None) -> _SemanticMemoryStore:
    store_path = tmp_path / "semantic-memory.sqlite3"
    return _load_store_factory()(store_path, event_bus=event_bus)


def _make_memory(
    pattern_id: str,
    *,
    version: int = 1,
    category: str = "research",
    pattern: str = "generalize repeated lessons",
    conditions: list[str] | None = None,
    actions: list[str] | None = None,
    success_rate: float = 0.5,
    confidence: float = 0.5,
    source_episodes: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> SemanticMemory:
    payload_metadata = {
        "confidence": confidence,
        "source_episodes": source_episodes if source_episodes is not None else ["ep-1", "ep-2"],
    }
    if metadata is not None:
        payload_metadata.update(metadata)

    return SemanticMemory(
        version=version,
        pattern_id=pattern_id,
        category=category,
        pattern=pattern,
        conditions=conditions if conditions is not None else ["condition-a", "condition-b"],
        actions=actions if actions is not None else ["action-a", "action-b"],
        success_rate=success_rate,
        metadata=payload_metadata,
    )


def _record_memory(value: object) -> SemanticMemory:
    if hasattr(value, "memory"):
        memory = getattr(value, "memory")
        if isinstance(memory, SemanticMemory):
            return memory
    if isinstance(value, SemanticMemory):
        return value
    if isinstance(value, dict):
        candidate = cast(dict[str, Any], value)
        if "memory" in candidate and isinstance(candidate["memory"], dict):
            return SemanticMemory.model_validate(cast(dict[str, Any], candidate["memory"]))
        return SemanticMemory.model_validate(candidate)
    raise AssertionError(f"Unsupported SemanticMemory shape: {type(value)!r}")


def _normalized_semantic_dump(memory: SemanticMemory) -> dict[str, Any]:
    dumped = memory.model_dump(mode="json")
    metadata = dict(dumped["metadata"])
    metadata.pop("active", None)
    dumped["metadata"] = metadata
    return dumped


def test_create_get_update_deactivate_and_delete_are_enforced(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    updated_at = datetime(2026, 1, 2, 10, 30, tzinfo=UTC)

    original = _make_memory(
        "pattern-1",
        category="research",
        pattern="prefer short summaries for repeated research tasks",
        conditions=["repeated research", "short summary"],
        actions=["summarize", "store notes"],
        success_rate=0.62,
        confidence=0.30,
        source_episodes=["ep-1", "ep-2"],
        metadata={"notes": "initial"},
    )
    store.create(original)

    created = store.get(original.pattern_id)
    assert _normalized_semantic_dump(_record_memory(created)) == _normalized_semantic_dump(original)
    assert getattr(created, "active") is True

    store.update(
        original.pattern_id,
        pattern="prefer concise summaries for repeated research tasks",
        conditions=["repeated research", "concise summary"],
        actions=["summarize", "store notes", "link evidence"],
        success_rate=0.7,
        metadata={"notes": "updated", "confidence": 0.35, "source_episodes": ["ep-1", "ep-2", "ep-3"]},
        updated_at=updated_at,
    )

    updated = store.read(original.pattern_id)
    assert isinstance(updated, SemanticMemory)
    assert updated.pattern == "prefer concise summaries for repeated research tasks"
    assert updated.conditions == ["repeated research", "concise summary"]
    assert updated.actions == ["summarize", "store notes", "link evidence"]
    assert updated.success_rate == 0.7
    assert _normalized_semantic_dump(updated)["metadata"] == {
        "notes": "updated",
        "confidence": 0.35,
        "source_episodes": ["ep-1", "ep-2", "ep-3"],
    }

    deactivated = store.deactivate(original.pattern_id)
    assert getattr(deactivated, "active") is False
    assert _normalized_semantic_dump(_record_memory(store.get(original.pattern_id))) == _normalized_semantic_dump(updated)

    with pytest.raises(PermissionError):
        store.delete(original.pattern_id)

    with pytest.raises(ValueError):
        store.create(original)


def test_search_filters_active_records_and_sorts_by_score(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    low = _make_memory(
        "pattern-low",
        category="research",
        success_rate=0.45,
        confidence=0.10,
    )
    high = _make_memory(
        "pattern-high",
        category="ops",
        success_rate=0.72,
        confidence=0.25,
    )
    medium = _make_memory(
        "pattern-medium",
        category="support",
        success_rate=0.50,
        confidence=0.20,
    )
    inactive = _make_memory(
        "pattern-inactive",
        category="archive",
        success_rate=0.99,
        confidence=0.99,
        metadata={"active": False},
    )

    store.create(low)
    store.create(high)
    store.create(medium)
    store.create(inactive)
    store.deactivate(inactive.pattern_id)

    results = store.search(category="ops")
    assert [item.pattern_id for item in results] == ["pattern-high"]
    assert "pattern-inactive" not in {item.pattern_id for item in results}

    all_results = store.search(category="research", active_only=False)
    assert [item.pattern_id for item in all_results] == ["pattern-low"]

    ranked = store.search(active_only=False)
    assert [item.pattern_id for item in ranked] == [
        "pattern-inactive",
        "pattern-high",
        "pattern-medium",
        "pattern-low",
    ]


def test_conflict_resolution_prefers_more_source_episodes_then_newer_and_emits_event(
    tmp_path: Path,
) -> None:
    events: list[object] = []
    bus = EventBus()
    bus.subscribe(EventType.conflict_detected, events.append)
    store = _make_store(tmp_path, event_bus=bus)

    older_fewer = _make_memory(
        "pattern-older-fewer",
        category="ops",
        pattern="prefer local cache over remote fetch",
        success_rate=0.5,
        confidence=0.2,
        source_episodes=["ep-1"],
    )
    newer_more = _make_memory(
        "pattern-newer-more",
        category="ops",
        pattern="prefer remote fetch when cache is stale",
        success_rate=0.55,
        confidence=0.3,
        source_episodes=["ep-1", "ep-2", "ep-3"],
    )
    tie_older = _make_memory(
        "pattern-tie-older",
        category="research",
        pattern="capture analysis notes before summarizing",
        success_rate=0.4,
        confidence=0.2,
        source_episodes=["ep-4", "ep-5"],
    )
    tie_newer = _make_memory(
        "pattern-tie-newer",
        category="research",
        pattern="capture analysis notes after summarizing",
        success_rate=0.45,
        confidence=0.25,
        source_episodes=["ep-6", "ep-7"],
    )

    store.create(older_fewer)
    store.create(newer_more)
    store.create(tie_older)
    store.create(tie_newer)

    ops_results = store.search(category="ops")
    research_results = store.search(category="research")

    assert [item.pattern_id for item in ops_results] == ["pattern-newer-more"]
    assert [item.pattern_id for item in research_results] == ["pattern-tie-newer"]

    assert getattr(store.get("pattern-older-fewer"), "active") is False
    assert getattr(store.get("pattern-newer-more"), "active") is True
    assert getattr(store.get("pattern-tie-older"), "active") is False
    assert getattr(store.get("pattern-tie-newer"), "active") is True

    conflict_events = [event for event in events if getattr(event, "event_type", None) == EventType.conflict_detected]
    assert len(conflict_events) >= 2
    assert all(getattr(event, "event_type", None) == EventType.conflict_detected for event in conflict_events)
    assert all("winner_pattern_id" in getattr(event, "payload", {}) for event in conflict_events)
