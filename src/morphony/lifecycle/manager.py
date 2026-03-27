from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from morphony.events import Event, EventBus, EventType
from morphony.models import TaskState

from .state_machine import TERMINAL_STATES, InvalidTransitionError, assert_valid_transition
from .store import LifecycleSnapshot, LifecycleStore, TaskLifecycleRecord, TransitionRecord


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TaskLifecycleManager:
    def __init__(self, store_path: str | Path, event_bus: EventBus | None = None) -> None:
        self._store = LifecycleStore(store_path)
        self._event_bus = event_bus
        self._snapshot = self._store.load()
        self._repair_invariants()
        self._persist()

    @property
    def pending_queue(self) -> list[str]:
        return list(self._snapshot.pending_queue)

    @property
    def running_task_id(self) -> str | None:
        return self._snapshot.running_task_id

    def submit_task(self, task_id: str, *, start_immediately: bool = True) -> str:
        if not task_id:
            raise ValueError("task_id must not be empty")
        if task_id in self._snapshot.tasks:
            raise ValueError(f"Task already exists: {task_id}")

        self._snapshot.tasks[task_id] = TaskLifecycleRecord(state=TaskState.pending)
        if not start_immediately:
            self._snapshot.pending_queue.append(task_id)
            self._persist()
            return task_id

        if self._snapshot.running_task_id is None:
            self._apply_transition(
                task_id=task_id,
                to_state=TaskState.running,
                reason="auto_start",
                trigger_queue_drain=False,
            )
        else:
            self._snapshot.pending_queue.append(task_id)
            self._persist()
        return task_id

    def transition(self, task_id: str, to_state: TaskState) -> TaskState:
        if task_id not in self._snapshot.tasks:
            raise KeyError(f"Unknown task: {task_id}")
        current_state = self._snapshot.tasks[task_id].state
        if (
            to_state == TaskState.running
            and self._snapshot.running_task_id is not None
            and self._snapshot.running_task_id != task_id
        ):
            raise InvalidTransitionError(
                f"Cannot transition task '{task_id}' to running while "
                f"'{self._snapshot.running_task_id}' is running"
            )
        assert_valid_transition(current_state, to_state)
        self._apply_transition(task_id=task_id, to_state=to_state, reason="manual")
        return self._snapshot.tasks[task_id].state

    def get_task_state(self, task_id: str) -> TaskState:
        if task_id not in self._snapshot.tasks:
            raise KeyError(f"Unknown task: {task_id}")
        return self._snapshot.tasks[task_id].state

    def get_transition_history(self, task_id: str) -> list[TransitionRecord]:
        if task_id not in self._snapshot.tasks:
            raise KeyError(f"Unknown task: {task_id}")
        return list(self._snapshot.tasks[task_id].history)

    def list_task_states(self) -> dict[str, TaskState]:
        return {
            task_id: record.state
            for task_id, record in self._snapshot.tasks.items()
        }

    def _apply_transition(
        self,
        *,
        task_id: str,
        to_state: TaskState,
        reason: str,
        trigger_queue_drain: bool = True,
    ) -> None:
        record = self._snapshot.tasks[task_id]
        from_state = record.state
        assert_valid_transition(from_state, to_state)

        timestamp = _utc_now()
        record.state = to_state
        record.history.append(
            TransitionRecord(from_state=from_state, to_state=to_state, timestamp=timestamp)
        )

        if to_state == TaskState.running:
            self._snapshot.running_task_id = task_id
            self._snapshot.pending_queue = [
                queued_task_id
                for queued_task_id in self._snapshot.pending_queue
                if queued_task_id != task_id
            ]

        if (
            self._snapshot.running_task_id == task_id
            and to_state in TERMINAL_STATES
        ):
            self._snapshot.running_task_id = None

        self._persist()
        self._emit_state_changed(task_id, from_state, to_state, reason, timestamp)

        if trigger_queue_drain and from_state == TaskState.running and to_state in TERMINAL_STATES:
            self._auto_start_next_pending_task()

    def _auto_start_next_pending_task(self) -> None:
        if self._snapshot.running_task_id is not None:
            return

        while self._snapshot.pending_queue:
            next_task_id = self._snapshot.pending_queue.pop(0)
            next_record = self._snapshot.tasks.get(next_task_id)
            if next_record is None:
                continue
            if next_record.state != TaskState.pending:
                continue
            self._apply_transition(
                task_id=next_task_id,
                to_state=TaskState.running,
                reason="queue_auto_start",
                trigger_queue_drain=False,
            )
            return

        self._persist()

    def _emit_state_changed(
        self,
        task_id: str,
        from_state: TaskState,
        to_state: TaskState,
        reason: str,
        timestamp: datetime,
    ) -> None:
        if self._event_bus is None:
            return
        self._event_bus.publish_sync(
            Event(
                task_id=task_id,
                event_type=EventType.state_changed,
                timestamp=timestamp,
                payload={
                    "from_state": from_state.value,
                    "to_state": to_state.value,
                    "reason": reason,
                },
            )
        )

    def _persist(self) -> None:
        self._store.save(self._snapshot)

    def _repair_invariants(self) -> None:
        snapshot: LifecycleSnapshot = self._snapshot
        snapshot.pending_queue = [
            task_id
            for task_id in snapshot.pending_queue
            if task_id in snapshot.tasks and snapshot.tasks[task_id].state == TaskState.pending
        ]

        if snapshot.running_task_id is not None:
            running_record = snapshot.tasks.get(snapshot.running_task_id)
            if running_record is None or running_record.state != TaskState.running:
                snapshot.running_task_id = None

        if snapshot.running_task_id is None:
            for task_id, record in snapshot.tasks.items():
                if record.state == TaskState.running:
                    snapshot.running_task_id = task_id
                    break

        if snapshot.running_task_id is None:
            self._auto_start_next_pending_task()
