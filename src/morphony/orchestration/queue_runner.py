from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from morphony.events import EventBus
from morphony.lifecycle import TaskLifecycleManager
from morphony.lifecycle.store import LifecycleStore


@dataclass(slots=True)
class QueueRunResult:
    before_running_task_id: str | None
    after_running_task_id: str | None
    before_pending_queue: list[str]
    after_pending_queue: list[str]

    @property
    def started_task_id(self) -> str | None:
        if self.before_running_task_id == self.after_running_task_id:
            return None
        return self.after_running_task_id


class QueueRunner:
    def __init__(
        self,
        lifecycle_store: str | Path,
        event_bus: EventBus | None = None,
    ) -> None:
        self._lifecycle_store = Path(lifecycle_store)
        self._event_bus = event_bus

    def run_once(self) -> QueueRunResult:
        before_snapshot = LifecycleStore(self._lifecycle_store).load()
        before_running_task_id = before_snapshot.running_task_id
        before_pending_queue = list(before_snapshot.pending_queue)

        runner_manager = TaskLifecycleManager(self._lifecycle_store, event_bus=self._event_bus)
        after_running_task_id = runner_manager.running_task_id
        after_pending_queue = runner_manager.pending_queue

        return QueueRunResult(
            before_running_task_id=before_running_task_id,
            after_running_task_id=after_running_task_id,
            before_pending_queue=before_pending_queue,
            after_pending_queue=after_pending_queue,
        )
