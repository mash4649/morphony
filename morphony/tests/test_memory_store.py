from __future__ import annotations

import importlib
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import pytest

from morphony.models import EpisodicMemory, TaskState


_MEMORY_FIELDS = (
    "version",
    "task_id",
    "goal",
    "plan",
    "steps",
    "result",
    "execution_state",
    "metadata",
)


class _EpisodicMemoryStore(Protocol):
    def create(self, memory: EpisodicMemory) -> object: ...

    def get(self, task_id: str) -> object: ...

    def update(self, task_id: str, **changes: Any) -> object: ...

    def search(
        self,
        *,
        task_id: str | None = None,
        goal_query: str | None = None,
        category: str | None = None,
        from_: datetime | None = None,
        to: datetime | None = None,
    ) -> Iterable[object]: ...

    def delete(self, task_id: str) -> object: ...


class _EpisodicMemoryStoreFactory(Protocol):
    def __call__(self, path: str | Path) -> _EpisodicMemoryStore: ...


def _load_store_factory() -> _EpisodicMemoryStoreFactory:
    for module_name in ("morphony.memory.store", "morphony.memory_store", "morphony.memory"):
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        store_cls = getattr(module, "EpisodicMemoryStore", None)
        if store_cls is not None:
            return cast(_EpisodicMemoryStoreFactory, store_cls)
    raise ModuleNotFoundError("EpisodicMemoryStore is not available yet")


def _make_store(tmp_path: Path) -> _EpisodicMemoryStore:
    store_path = tmp_path / "episodic-memory.sqlite3"
    return _load_store_factory()(store_path)


def _make_memory(
    task_id: str,
    *,
    version: int = 1,
    goal: str = "collect evidence for the requested task",
    plan: list[str] | None = None,
    steps: list[dict[str, Any]] | None = None,
    result: Any = None,
    execution_state: TaskState = TaskState.pending,
    category: str = "research",
    metadata: dict[str, Any] | None = None,
) -> EpisodicMemory:
    payload_metadata = {"category": category, "source": "unit-test"}
    if metadata is not None:
        payload_metadata.update(metadata)

    return EpisodicMemory(
        version=version,
        task_id=task_id,
        goal=goal,
        plan=plan if plan is not None else ["scope", "research", "report"],
        steps=steps if steps is not None else [{"step": 1, "status": "done"}],
        result=result if result is not None else {"status": "draft", "score": 0.5},
        execution_state=execution_state,
        metadata=payload_metadata,
    )


def _memory_payload(value: object) -> dict[str, Any]:
    candidate: object = value

    memory_attr = getattr(candidate, "memory", None)
    if isinstance(memory_attr, EpisodicMemory):
        candidate = memory_attr

    if isinstance(candidate, EpisodicMemory):
        return candidate.model_dump(mode="python")

    if isinstance(candidate, dict):
        typed_value: dict[str, Any] = cast(dict[str, Any], candidate)
        candidate_dict: dict[str, Any]
        nested_memory = typed_value.get("memory")
        if isinstance(nested_memory, dict):
            candidate_dict = cast(dict[str, Any], nested_memory)
        else:
            candidate_dict = typed_value

        if all(field in candidate_dict for field in _MEMORY_FIELDS):
            memory_payload: dict[str, Any] = {
                field: candidate_dict[field] for field in _MEMORY_FIELDS
            }
            memory = EpisodicMemory.model_validate(memory_payload)
            return memory.model_dump(mode="python")

    attr_source = cast(Any, candidate)
    model_dump = getattr(attr_source, "model_dump", None)
    if callable(model_dump):
        dumped: object = model_dump(mode="python")
        if isinstance(dumped, dict):
            dumped_dict: dict[str, Any] = cast(dict[str, Any], dumped)
            if all(field in dumped_dict for field in _MEMORY_FIELDS):
                memory_payload: dict[str, Any] = {
                    field: dumped_dict[field] for field in _MEMORY_FIELDS
                }
                memory = EpisodicMemory.model_validate(memory_payload)
                return memory.model_dump(mode="python")

    raise AssertionError(f"Unsupported EpisodicMemory shape: {type(value)!r}")


def _memory_value(value: object) -> EpisodicMemory:
    return EpisodicMemory.model_validate(_memory_payload(value))


def _task_ids(values: object) -> set[str]:
    if isinstance(values, list):
        items: list[object] = cast(list[object], values)
    else:
        items = list(cast(Iterable[object], values))
    return {_memory_value(item).task_id for item in items}


def test_create_get_update_round_trips_all_fields(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    original = _make_memory(
        task_id="task-roundtrip",
        version=7,
        goal="collect evidence for the roundtrip test",
        plan=["discover", "validate", "summarize"],
        steps=[
            {"step": 1, "name": "discover", "status": "done", "details": {"count": 2}},
            {"step": 2, "name": "validate", "status": "done", "details": {"count": 1}},
        ],
        result={"summary": "initial", "metrics": {"coverage": 0.75, "confidence": 0.6}},
        execution_state=TaskState.paused,
        metadata={"category": "research", "source": "unit-test", "revision": 1},
    )

    store.create(original)

    created = _memory_value(store.get(original.task_id))
    assert created.model_dump(mode="json") == original.model_dump(mode="json")

    store.update(
        original.task_id,
        goal="collect evidence for the updated roundtrip test",
        plan=["discover", "validate", "write"],
        steps=[
            {"step": 1, "name": "discover", "status": "done", "details": {"count": 3}},
            {"step": 2, "name": "validate", "status": "done", "details": {"count": 2}},
            {"step": 3, "name": "write", "status": "pending"},
        ],
        result={"summary": "final", "metrics": {"coverage": 0.95, "confidence": 0.9}},
        execution_state=TaskState.running,
        metadata={"category": "research", "source": "unit-test", "revision": 2},
    )

    updated = _memory_value(store.get(original.task_id))
    assert updated.version == original.version
    assert updated.task_id == original.task_id
    assert updated.goal == "collect evidence for the updated roundtrip test"
    assert updated.plan == ["discover", "validate", "write"]
    assert updated.steps == [
        {"step": 1, "name": "discover", "status": "done", "details": {"count": 3}},
        {"step": 2, "name": "validate", "status": "done", "details": {"count": 2}},
        {"step": 3, "name": "write", "status": "pending"},
    ]
    assert updated.result == {"summary": "final", "metrics": {"coverage": 0.95, "confidence": 0.9}}
    assert updated.execution_state == TaskState.running
    assert updated.metadata == {"category": "research", "source": "unit-test", "revision": 2}


def test_delete_is_rejected_and_record_remains(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    memory = _make_memory(task_id="task-delete", category="ops")

    store.create(memory)

    with pytest.raises(Exception):
        store.delete(memory.task_id)

    assert _memory_value(store.get(memory.task_id)).task_id == memory.task_id


def test_search_supports_exact_task_similarity_category_and_period_filters(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    exact = _make_memory(
        task_id="task-001",
        goal="battery supply chain risks",
        category="research",
        execution_state=TaskState.completed,
    )
    similar = _make_memory(
        task_id="task-001-extra",
        goal="battery supply chain shortlist",
        category="research",
        execution_state=TaskState.failed,
    )
    period = _make_memory(
        task_id="task-period",
        goal="plan garden irrigation",
        category="ops",
        execution_state=TaskState.pending,
    )

    store.create(exact)
    store.create(similar)

    exact_results = _task_ids(store.search(task_id="task-001"))
    assert exact_results == {"task-001"}

    goal_results = _task_ids(store.search(goal_query="battery supply chain"))
    assert {"task-001", "task-001-extra"} <= goal_results
    assert "task-period" not in goal_results

    category_results = _task_ids(store.search(category="research"))
    assert category_results == {"task-001", "task-001-extra"}

    before_create = datetime.now(UTC)
    store.create(period)
    after_create = datetime.now(UTC)

    created_window_results = _task_ids(
        store.search(category="ops", from_=before_create, to=after_create)
    )
    assert created_window_results == {"task-period"}

    before_update = datetime.now(UTC)
    store.update(
        period.task_id,
        goal="plan garden irrigation and watering",
        result={"summary": "updated"},
        execution_state=TaskState.running,
    )
    after_update = datetime.now(UTC)

    updated_window_results = _task_ids(
        store.search(category="ops", from_=before_update, to=after_update)
    )
    assert updated_window_results == {"task-period"}
