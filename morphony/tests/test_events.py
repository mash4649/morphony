from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from morphony.events import (
    AuditLogReader,
    AuditLogWriter,
    Event,
    EventBus,
    EventType,
    protect_audit_log_permissions,
)


@pytest.mark.asyncio
async def test_event_bus_publish_invokes_sync_and_async_subscribers() -> None:
    bus = EventBus()
    calls: list[tuple[str, str]] = []

    def sync_handler(event: Event) -> None:
        calls.append(("sync", event.task_id))

    async def async_handler(event: Event) -> None:
        calls.append(("async", event.task_id))

    bus.subscribe(EventType.task_started, sync_handler)
    bus.subscribe(EventType.task_started, async_handler)

    await bus.publish(
        Event(
            task_id="task-1",
            event_type=EventType.task_started,
            payload={"step": 1},
        )
    )

    assert len(calls) == 2
    assert {kind for kind, _ in calls} == {"sync", "async"}
    assert {task_id for _, task_id in calls} == {"task-1"}


def test_audit_log_is_timestamped_and_append_only(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.log"
    writer = AuditLogWriter(log_path)

    first = Event(
        task_id="task-1",
        event_type=EventType.task_started,
        timestamp=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        payload={"stage": 1},
    )
    second = Event(
        task_id="task-1",
        event_type=EventType.step_completed,
        timestamp=datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
        payload={"stage": 2},
    )

    writer.append(first)
    snapshot_before = log_path.read_text(encoding="utf-8")
    lines_before = [line for line in snapshot_before.splitlines() if line.strip()]
    assert len(lines_before) == 1
    assert "timestamp" in json.loads(lines_before[0])

    writer.append(second)
    snapshot_after = log_path.read_text(encoding="utf-8")
    lines_after = [line for line in snapshot_after.splitlines() if line.strip()]

    assert snapshot_after.startswith(snapshot_before)
    assert len(lines_after) == 2
    for line in lines_after:
        record = json.loads(line)
        assert record["timestamp"]


def test_audit_log_reader_filters_by_task_type_and_time_range(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.log"
    writer = AuditLogWriter(log_path)
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    writer.append(
        Event(
            task_id="task-1",
            event_type=EventType.task_started,
            timestamp=base,
            payload={"step": 1},
        )
    )
    writer.append(
        Event(
            task_id="task-1",
            event_type=EventType.step_completed,
            timestamp=base + timedelta(minutes=10),
            payload={"step": 2},
        )
    )
    writer.append(
        Event(
            task_id="task-2",
            event_type=EventType.task_started,
            timestamp=base + timedelta(minutes=20),
            payload={"step": 3},
        )
    )

    reader = AuditLogReader(log_path)

    by_task = reader.read(task_id="task-1")
    assert len(by_task) == 2
    assert {entry.event_type for entry in by_task} == {
        EventType.task_started,
        EventType.step_completed,
    }

    by_type = reader.read(event_type=EventType.task_started)
    assert len(by_type) == 2
    assert {entry.task_id for entry in by_type} == {"task-1", "task-2"}

    by_time = reader.read(
        from_=base + timedelta(minutes=5),
        to=base + timedelta(minutes=15),
    )
    assert len(by_time) == 1
    assert by_time[0].task_id == "task-1"
    assert by_time[0].event_type == EventType.step_completed


def test_permission_protection_blocks_overwrite_and_delete(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("permission checks are posix-specific")

    log_dir = tmp_path / "protected"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "audit.log"
    log_path.write_text("initial\n", encoding="utf-8")

    protect_audit_log_permissions(log_path, file_mode=0o400, directory_mode=0o500)

    try:
        with pytest.raises((PermissionError, OSError)):
            log_path.write_text("overwrite\n", encoding="utf-8")

        with pytest.raises((PermissionError, OSError)):
            log_path.unlink()

        assert log_path.exists()
        assert log_path.read_text(encoding="utf-8") == "initial\n"
    finally:
        log_path.chmod(0o600)
        log_dir.chmod(0o700)

