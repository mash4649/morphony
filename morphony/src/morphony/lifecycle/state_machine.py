from __future__ import annotations

from collections.abc import Mapping

from morphony.models import TaskState


class InvalidTransitionError(ValueError):
    """Raised when a task state transition is not allowed."""


VALID_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = {
    TaskState.pending: frozenset({TaskState.planning, TaskState.approved, TaskState.running, TaskState.stopped}),
    TaskState.planning: frozenset(
        {TaskState.pending, TaskState.approved, TaskState.running, TaskState.failed, TaskState.stopped}
    ),
    TaskState.approved: frozenset({TaskState.pending, TaskState.running, TaskState.failed, TaskState.stopped}),
    TaskState.running: frozenset(
        {
            TaskState.paused,
            TaskState.suspended,
            TaskState.completed,
            TaskState.failed,
            TaskState.stopped,
        }
    ),
    TaskState.paused: frozenset({TaskState.running, TaskState.stopped, TaskState.failed}),
    TaskState.suspended: frozenset({TaskState.running, TaskState.stopped, TaskState.failed}),
    TaskState.completed: frozenset(),
    TaskState.failed: frozenset(),
    TaskState.stopped: frozenset(),
}

TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.completed,
        TaskState.failed,
        TaskState.stopped,
    }
)


def assert_valid_transition(from_state: TaskState, to_state: TaskState) -> None:
    if to_state not in VALID_TRANSITIONS[from_state]:
        raise InvalidTransitionError(
            f"Invalid transition: {from_state.value} -> {to_state.value}"
        )

