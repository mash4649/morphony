from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .types import Event, EventType


def _ensure_directory(path: Path, mode: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(mode)


def protect_directory_permissions(path: str | Path, mode: int = 0o700) -> Path:
    directory = Path(path)
    _ensure_directory(directory, mode)
    return directory


def protect_file_permissions(path: str | Path, mode: int = 0o600) -> Path:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.touch(exist_ok=True)
    file_path.chmod(mode)
    return file_path


def protect_audit_log_permissions(
    path: str | Path,
    *,
    file_mode: int = 0o600,
    directory_mode: int = 0o700,
) -> Path:
    file_path = Path(path)
    protect_directory_permissions(file_path.parent, mode=directory_mode)
    protect_file_permissions(file_path, mode=file_mode)
    return file_path


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp filters must be timezone-aware")
    return value.astimezone(UTC)


def _parse_timestamp(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    return _to_utc(parsed)


class AuditLogWriter:
    def __init__(
        self,
        path: str | Path,
        *,
        auto_harden_permissions: bool = True,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        if auto_harden_permissions:
            self.harden_permissions()

    def harden_permissions(
        self,
        *,
        file_mode: int = 0o600,
        directory_mode: int = 0o700,
    ) -> None:
        protect_audit_log_permissions(
            self.path,
            file_mode=file_mode,
            directory_mode=directory_mode,
        )

    def append(self, event: Event) -> None:
        record: dict[str, Any] = event.model_dump(mode="json")
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write(os.linesep)


class AuditLogReader:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def iter_events(
        self,
        *,
        task_id: str | None = None,
        event_type: EventType | str | None = None,
        from_: datetime | None = None,
        to: datetime | None = None,
    ) -> list[Event]:
        return list(
            self._iter_event_stream(
                task_id=task_id,
                event_type=event_type,
                from_=from_,
                to=to,
            )
        )

    def read(
        self,
        *,
        task_id: str | None = None,
        event_type: EventType | str | None = None,
        from_: datetime | None = None,
        to: datetime | None = None,
    ) -> list[Event]:
        return self.iter_events(
            task_id=task_id,
            event_type=event_type,
            from_=from_,
            to=to,
        )

    def _iter_event_stream(
        self,
        *,
        task_id: str | None,
        event_type: EventType | str | None,
        from_: datetime | None,
        to: datetime | None,
    ) -> list[Event]:
        if not self.path.exists():
            return []

        selected_event_type = (
            event_type if event_type is None or isinstance(event_type, EventType) else EventType(event_type)
        )
        from_ts = _to_utc(from_) if from_ is not None else None
        to_ts = _to_utc(to) if to is not None else None

        events: list[Event] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                payload = json.loads(stripped)
                event = Event(
                    task_id=payload["task_id"],
                    event_type=EventType(payload["event_type"]),
                    timestamp=_parse_timestamp(str(payload["timestamp"])),
                    payload=payload["payload"],
                )
                if task_id is not None and event.task_id != task_id:
                    continue
                if selected_event_type is not None and event.event_type != selected_event_type:
                    continue
                if from_ts is not None and event.timestamp < from_ts:
                    continue
                if to_ts is not None and event.timestamp > to_ts:
                    continue
                events.append(event)
        return events


__all__ = [
    "AuditLogReader",
    "AuditLogWriter",
    "protect_audit_log_permissions",
    "protect_directory_permissions",
    "protect_file_permissions",
]
