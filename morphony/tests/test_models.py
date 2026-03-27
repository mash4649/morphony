from __future__ import annotations

from morphony.models import (
    EpisodicMemory,
    EscalationLevel,
    SemanticMemory,
    TaskState,
)


EXPECTED_TASK_STATE_VALUES = {
    "pending",
    "planning",
    "approved",
    "running",
    "paused",
    "suspended",
    "completed",
    "failed",
    "stopped",
}

EXPECTED_ESCALATION_LEVEL_VALUES = {"L1", "L2", "L3"}


def test_enums_and_memory_models():
    assert {state.value for state in TaskState} == EXPECTED_TASK_STATE_VALUES
    assert {level.value for level in EscalationLevel} == EXPECTED_ESCALATION_LEVEL_VALUES

    episodic = EpisodicMemory(
        task_id="task-1",
        goal="minimal test goal",
        execution_state=TaskState.pending,
    )
    semantic = SemanticMemory(
        pattern_id="pattern-1",
        category="general",
        pattern="minimal test pattern",
        success_rate=0.5,
    )

    assert episodic.version == 1
    assert semantic.version == 1
