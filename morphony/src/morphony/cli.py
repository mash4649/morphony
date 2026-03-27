from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import cast
from uuid import uuid4

import typer
import yaml
from rich.console import Console
from rich.table import Table

from morphony.config import (
    DEFAULT_CONFIG_PATH,
    AgentConfig,
    apply_runtime_overrides,
    load_config,
    parse_runtime_overrides,
)
from morphony.events import AuditLogReader, AuditLogWriter, EventBus, EventType
from morphony.lifecycle import (
    CheckpointCorruptedError,
    CheckpointManager,
    InvalidTransitionError,
    TaskLifecycleManager,
)
from morphony.models import AutonomyLevel, EpisodicMemory, TaskState, Tool, TrustScore
from morphony.observability import ObservabilityEngine
from morphony.improvement import ImprovementLoopEngine
from morphony.orchestration import QueueRunner
from morphony.review import ReviewEngine, SelfEvaluationEngine
from morphony.memory import EpisodicMemoryStore, MemoryPatternExtractor, SemanticMemoryStore
from morphony.safety import EscalationEngine, SafetyController
from morphony.trust import TrustScoreCalculator, TrustScoreStore
from morphony.tools import LlmAnalyzeTool, ReportRenderTool, ToolRegistry, WebFetchTool, WebSearchTool

app = typer.Typer(help="Morphony agent command line interface.")
config_app = typer.Typer(help="Configuration management commands.", no_args_is_help=True)
tool_app = typer.Typer(help="Tool management commands.", no_args_is_help=True)
memory_app = typer.Typer(help="Memory management commands.", no_args_is_help=True)
queue_app = typer.Typer(help="Queue orchestration commands.", no_args_is_help=True)
review_app = typer.Typer(help="Review commands.", no_args_is_help=True)
trust_app = typer.Typer(help="Trust score commands.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(tool_app, name="tool")
app.add_typer(memory_app, name="memory")
app.add_typer(queue_app, name="queue")
app.add_typer(review_app, name="review")
app.add_typer(trust_app, name="trust")
console = Console()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _load_task_registry(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    raw_text = path.read_text(encoding="utf-8")
    if not raw_text.strip():
        return {}

    parsed = json.loads(raw_text)
    if not isinstance(parsed, dict):
        raise ValueError(f"Invalid task registry at {path}: expected JSON object")

    typed_parsed = cast(dict[object, object], parsed)
    registry: dict[str, dict[str, object]] = {}
    for raw_task_id, raw_payload in typed_parsed.items():
        if not isinstance(raw_task_id, str):
            continue
        if not isinstance(raw_payload, dict):
            continue
        registry[raw_task_id] = cast(dict[str, object], raw_payload)
    return registry


def _save_task_registry(path: Path, registry: dict[str, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(f"{serialized}\n", encoding="utf-8")


def _register_task(path: Path, task_id: str, goal: str, now: datetime) -> None:
    registry = _load_task_registry(path)
    registry[task_id] = {
        "goal": goal,
        "created_at": _timestamp_text(now),
    }
    _save_task_registry(path, registry)


def _task_goal(registry: dict[str, dict[str, object]], task_id: str) -> str | None:
    payload = registry.get(task_id)
    if payload is None:
        return None
    goal = payload.get("goal")
    if not isinstance(goal, str):
        return None
    return goal


def _load_episodic_memory_batch(source: Path) -> list[EpisodicMemory]:
    raw_text = source.read_text(encoding="utf-8")
    if not raw_text.strip():
        return []
    parsed = yaml.safe_load(raw_text)
    if parsed is None:
        return []

    payloads: list[object]
    if isinstance(parsed, list):
        payloads = list(parsed)
    else:
        payloads = [parsed]

    memories: list[EpisodicMemory] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        candidate = cast(dict[str, object], payload)
        if "episodic_memory" in candidate and isinstance(candidate["episodic_memory"], dict):
            candidate = cast(dict[str, object], candidate["episodic_memory"])
        migrated = _migrate_episodic_memory_payload(candidate)
        memories.append(EpisodicMemory.model_validate(migrated))
    return memories


def _migrate_episodic_memory_payload(payload: dict[str, object]) -> dict[str, object]:
    migrated = dict(payload)
    raw_version = migrated.get("version")
    if not isinstance(raw_version, int) or raw_version < 1:
        migrated["version"] = 1

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
        raw_state = migrated["execution_state"]
        if isinstance(raw_state, str):
            migrated["execution_state"] = TaskState(raw_state)
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


def _elapsed_seconds(manager: TaskLifecycleManager, task_id: str, now: datetime) -> float:
    history = manager.get_transition_history(task_id)
    if not history:
        return 0.0
    started_at = history[0].timestamp
    elapsed = (now - started_at).total_seconds()
    return elapsed if elapsed > 0 else 0.0


def _load_tool_plugin_manifest(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    raw_text = path.read_text(encoding="utf-8")
    if not raw_text.strip():
        return []
    parsed = json.loads(raw_text)
    if not isinstance(parsed, list):
        raise ValueError(f"Invalid tool manifest at {path}: expected JSON array")
    manifest: list[dict[str, str]] = []
    for item in cast(list[object], parsed):
        if not isinstance(item, dict):
            continue
        typed_item = cast(dict[str, object], item)
        raw_name = typed_item.get("name")
        raw_plugin_path = typed_item.get("plugin_path")
        if not isinstance(raw_name, str) or not isinstance(raw_plugin_path, str):
            continue
        manifest.append({"name": raw_name, "plugin_path": raw_plugin_path})
    return manifest


def _save_tool_plugin_manifest(path: Path, manifest: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(f"{serialized}\n", encoding="utf-8")


def _append_tool_plugin_manifest(path: Path, tool_name: str, plugin_path: Path) -> None:
    manifest = _load_tool_plugin_manifest(path)
    updated = False
    plugin_path_text = str(plugin_path.resolve())
    for entry in manifest:
        if entry["name"] == tool_name:
            entry["plugin_path"] = plugin_path_text
            updated = True
            break
    if not updated:
        manifest.append({"name": tool_name, "plugin_path": plugin_path_text})
    _save_tool_plugin_manifest(path, manifest)


def _builtin_tools() -> list[Tool]:
    return [
        WebSearchTool(),
        WebFetchTool(),
        LlmAnalyzeTool(),
        ReportRenderTool(),
    ]


def _load_plugin_module(plugin_path: Path) -> ModuleType:
    resolved = plugin_path.expanduser().resolve()
    if not resolved.exists():
        raise ValueError(f"Plugin path does not exist: {resolved}")
    if not resolved.is_file():
        raise ValueError(f"Plugin path must be a file: {resolved}")

    spec = importlib.util.spec_from_file_location(f"morphony_plugin_{uuid4().hex}", resolved)
    if spec is None or spec.loader is None:
        raise ValueError(f"Failed to load plugin module spec from {resolved}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _coerce_plugin_tool(candidate: object, plugin_path: Path) -> Tool:
    if isinstance(candidate, Tool):
        return candidate
    raise ValueError(
        f"Plugin at {plugin_path} must provide a Tool via `TOOL` or `create_tool()`"
    )


def _load_tool_from_plugin(plugin_path: Path) -> Tool:
    module = _load_plugin_module(plugin_path)
    if hasattr(module, "TOOL"):
        return _coerce_plugin_tool(getattr(module, "TOOL"), plugin_path)
    create_tool = getattr(module, "create_tool", None)
    if callable(create_tool):
        return _coerce_plugin_tool(create_tool(), plugin_path)
    raise ValueError(
        f"Plugin at {plugin_path} does not define `TOOL` or callable `create_tool`"
    )


@app.command("version")
def version() -> None:
    """Print package version."""
    from morphony import __version__

    console.print(f"morphony {__version__}")


@app.command("run")
def run(
    goal: str = typer.Argument(..., help="Goal text for the new task."),
    config_file: Path | None = typer.Option(
        None,
        "--config-file",
        "-c",
        help="Path to agent_config.yaml. Defaults to repository config path.",
    ),
    lifecycle_store: Path = typer.Option(
        Path("runtime/lifecycle.json"),
        "--lifecycle-store",
        help="Path to persisted lifecycle store.",
    ),
    checkpoint_dir: Path = typer.Option(
        Path("runtime/checkpoints"),
        "--checkpoint-dir",
        help="Directory containing task checkpoints.",
    ),
    task_registry: Path = typer.Option(
        Path("runtime/tasks.json"),
        "--task-registry",
        help="Path to task metadata registry.",
    ),
    memory_file: Path = typer.Option(
        Path("runtime/memory/episodic_feedback.jsonl"),
        "--memory-file",
        help="Path to episodic memory feedback JSONL file.",
    ),
) -> None:
    """Create a new task and return its task_id."""
    if not goal:
        raise typer.BadParameter("goal must not be empty")
    _ = checkpoint_dir, memory_file
    config = load_config(config_file)
    manager = TaskLifecycleManager(lifecycle_store)
    task_id = f"task-{uuid4().hex[:12]}"
    if config.autonomy_level == AutonomyLevel.plan_only:
        manager.submit_task(task_id, start_immediately=False)
        manager.transition(task_id, TaskState.planning)
    else:
        manager.submit_task(task_id)
    _register_task(task_registry, task_id, goal, _utc_now())
    state = manager.get_task_state(task_id)
    console.print(f"Created task '{task_id}' (state: {state.value}).")
    console.print(f"Goal: {goal}")
    if config.autonomy_level == AutonomyLevel.plan_only:
        console.print("Autonomy level: plan_only (execution deferred).")


@app.command("status")
def status(
    task_id: str | None = typer.Argument(None, help="Task ID to show detailed status."),
    lifecycle_store: Path = typer.Option(
        Path("runtime/lifecycle.json"),
        "--lifecycle-store",
        help="Path to persisted lifecycle store.",
    ),
    checkpoint_dir: Path = typer.Option(
        Path("runtime/checkpoints"),
        "--checkpoint-dir",
        help="Directory containing task checkpoints.",
    ),
    task_registry: Path = typer.Option(
        Path("runtime/tasks.json"),
        "--task-registry",
        help="Path to task metadata registry.",
    ),
    audit_log: Path = typer.Option(
        Path("runtime/audit/audit.log"),
        "--audit-log",
        help="Path to append-only audit log file.",
    ),
    memory_file: Path = typer.Option(
        Path("runtime/memory/episodic_feedback.jsonl"),
        "--memory-file",
        help="Path to episodic memory feedback JSONL file.",
    ),
    summary_dir: Path = typer.Option(
        Path("runtime/summaries"),
        "--summary-dir",
        help="Directory where task summaries are written.",
    ),
    config_file: Path | None = typer.Option(
        None,
        "--config-file",
        "-c",
        help="Path to agent_config.yaml. Defaults to repository config path.",
    ),
) -> None:
    """Show task list or a detailed task status view."""
    manager = TaskLifecycleManager(lifecycle_store)
    states = manager.list_task_states()
    registry = _load_task_registry(task_registry)

    if task_id is None:
        if not states:
            console.print("No tasks found.")
            return
        for listed_task_id in sorted(states):
            goal = _task_goal(registry, listed_task_id)
            goal_text = goal if goal else "(no goal)"
            console.print(f"{listed_task_id}\t{states[listed_task_id].value}\t{goal_text}")
        return

    if task_id not in states:
        raise typer.BadParameter(f"Unknown task: {task_id}")

    state = states[task_id]
    history = manager.get_transition_history(task_id)
    elapsed = _elapsed_seconds(manager, task_id, _utc_now())
    checkpoint = CheckpointManager(checkpoint_dir).load_checkpoint(task_id)
    goal = _task_goal(registry, task_id)
    observability = ObservabilityEngine(
        lifecycle_store=lifecycle_store,
        checkpoint_dir=checkpoint_dir,
        audit_log=audit_log,
        memory_file=memory_file,
        summary_dir=summary_dir,
        config_file=config_file,
    )
    report = observability.build_status(task_id, goal=goal)

    console.print(f"Task: {task_id}")
    console.print(f"State: {state.value}")
    if goal:
        console.print(f"Goal: {goal}")
    console.print(f"Elapsed seconds: {elapsed:.1f}")
    console.print(f"Transitions: {len(history)}")
    console.print(f"Completed steps: {report.completed_steps}/{report.total_steps}")
    console.print(
        "Budget remaining: "
        + (
            "-"
            if report.budget_remaining_ratio is None
            else f"{report.budget_remaining_ratio:.2f}"
        )
    )
    console.print(f"Escalations: {report.escalation_count}")
    console.print(f"Improvements: {report.improvement_count}")
    console.print(
        "Final score: "
        + ("-" if report.final_score is None else f"{report.final_score:.2f}")
    )

    if checkpoint is None:
        console.print("Checkpoint: none")
    else:
        console.print(f"Checkpoint version: {checkpoint.version}")
        console.print(f"Last completed step: {checkpoint.last_completed_step_id}")
        if checkpoint.budget_delta:
            console.print(f"Budget delta: {checkpoint.budget_delta}")
    if report.summary_path is not None:
        console.print(f"Summary: {report.summary_path}")


@app.command("watch")
def watch(
    task_id: str = typer.Argument(..., help="Task ID to watch."),
    lifecycle_store: Path = typer.Option(
        Path("runtime/lifecycle.json"),
        "--lifecycle-store",
        help="Path to persisted lifecycle store.",
    ),
    checkpoint_dir: Path = typer.Option(
        Path("runtime/checkpoints"),
        "--checkpoint-dir",
        help="Directory containing task checkpoints.",
    ),
    task_registry: Path = typer.Option(
        Path("runtime/tasks.json"),
        "--task-registry",
        help="Path to task metadata registry.",
    ),
    audit_log: Path = typer.Option(
        Path("runtime/audit/audit.log"),
        "--audit-log",
        help="Path to append-only audit log file.",
    ),
    memory_file: Path = typer.Option(
        Path("runtime/memory/episodic_feedback.jsonl"),
        "--memory-file",
        help="Path to episodic memory feedback JSONL file.",
    ),
    summary_dir: Path = typer.Option(
        Path("runtime/summaries"),
        "--summary-dir",
        help="Directory where task summaries are written.",
    ),
    config_file: Path | None = typer.Option(
        None,
        "--config-file",
        "-c",
        help="Path to agent_config.yaml. Defaults to repository config path.",
    ),
    event_type: str | None = typer.Option(
        None,
        "--event-type",
        help="Optional event type filter.",
    ),
    follow: bool = typer.Option(
        True,
        "--follow/--once",
        help="Continue watching for new events. Use --once to print current events and exit.",
    ),
    poll_interval_seconds: float = typer.Option(
        0.5,
        "--poll-interval-seconds",
        min=0.1,
        help="Polling interval used while following the audit log.",
    ),
    timeout_seconds: float | None = typer.Option(
        None,
        "--timeout-seconds",
        min=0.0,
        help="Optional maximum time to keep following the log.",
    ),
) -> None:
    """Watch task events from the audit log."""
    _ = lifecycle_store, checkpoint_dir, task_registry
    selected_event_type: EventType | None = None
    if event_type is not None:
        try:
            selected_event_type = EventType(event_type)
        except ValueError as exc:
            raise typer.BadParameter(f"Unknown event type: {event_type}") from exc

    registry = _load_task_registry(task_registry)
    goal = _task_goal(registry, task_id)
    observability = ObservabilityEngine(
        lifecycle_store=lifecycle_store,
        checkpoint_dir=checkpoint_dir,
        audit_log=audit_log,
        memory_file=memory_file,
        summary_dir=summary_dir,
        config_file=config_file,
    )
    events = observability.watch_events(
        task_id,
        event_type=selected_event_type,
        follow=follow,
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds,
        goal=goal,
    )
    if not events:
        console.print(f"No events found for task '{task_id}'.")
        return

    for event in events:
        payload_text = json.dumps(event.payload, ensure_ascii=False, sort_keys=True)
        console.print(
            f"{_timestamp_text(event.timestamp)}\t{event.task_id}\t{event.event_type.value}\t{payload_text}"
        )


@app.command("approve")
def approve(
    task_id: str = typer.Argument(..., help="Task ID to approve for running."),
    lifecycle_store: Path = typer.Option(
        Path("runtime/lifecycle.json"),
        "--lifecycle-store",
        help="Path to persisted lifecycle store.",
    ),
    checkpoint_dir: Path = typer.Option(
        Path("runtime/checkpoints"),
        "--checkpoint-dir",
        help="Directory containing task checkpoints.",
    ),
    task_registry: Path = typer.Option(
        Path("runtime/tasks.json"),
        "--task-registry",
        help="Path to task metadata registry.",
    ),
) -> None:
    """Approve a paused/pending task and move it to running when allowed."""
    _ = checkpoint_dir, task_registry
    manager = TaskLifecycleManager(lifecycle_store)
    try:
        current_state = manager.get_task_state(task_id)
    except KeyError as exc:
        raise typer.BadParameter(f"Unknown task: {task_id}") from exc

    if current_state == TaskState.running:
        console.print(f"Task '{task_id}' is already running.")
        return
    if current_state in {TaskState.completed, TaskState.failed, TaskState.stopped}:
        console.print(f"Task '{task_id}' is terminal ({current_state.value}) and cannot be approved.")
        return

    try:
        if current_state in {TaskState.pending, TaskState.planning}:
            manager.transition(task_id, TaskState.approved)
            console.print(f"Task '{task_id}' approved and moved to approved.")
            return
        manager.transition(task_id, TaskState.running)
    except InvalidTransitionError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"Task '{task_id}' approved and moved to running.")


@app.command("commit")
def commit(
    task_id: str = typer.Argument(..., help="Task ID to commit after approval."),
    lifecycle_store: Path = typer.Option(
        Path("runtime/lifecycle.json"),
        "--lifecycle-store",
        help="Path to persisted lifecycle store.",
    ),
    checkpoint_dir: Path = typer.Option(
        Path("runtime/checkpoints"),
        "--checkpoint-dir",
        help="Directory containing task checkpoints.",
    ),
    task_registry: Path = typer.Option(
        Path("runtime/tasks.json"),
        "--task-registry",
        help="Path to task metadata registry.",
    ),
) -> None:
    """Commit an approved task and move it to running."""
    _ = checkpoint_dir, task_registry
    manager = TaskLifecycleManager(lifecycle_store)
    try:
        current_state = manager.get_task_state(task_id)
    except KeyError as exc:
        raise typer.BadParameter(f"Unknown task: {task_id}") from exc

    if current_state == TaskState.running:
        console.print(f"Task '{task_id}' is already running.")
        return
    if current_state != TaskState.approved:
        raise typer.BadParameter(
            f"Task '{task_id}' must be approved to commit (current: {current_state.value})."
        )

    try:
        manager.transition(task_id, TaskState.running)
    except InvalidTransitionError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"Task '{task_id}' committed and moved to running.")


@app.command("reject")
def reject(
    task_id: str = typer.Argument(..., help="Task ID to reject."),
    reason: str = typer.Option(..., "--reason", help="Reason for rejection."),
    lifecycle_store: Path = typer.Option(
        Path("runtime/lifecycle.json"),
        "--lifecycle-store",
        help="Path to persisted lifecycle store.",
    ),
    checkpoint_dir: Path = typer.Option(
        Path("runtime/checkpoints"),
        "--checkpoint-dir",
        help="Directory containing task checkpoints.",
    ),
    task_registry: Path = typer.Option(
        Path("runtime/tasks.json"),
        "--task-registry",
        help="Path to task metadata registry.",
    ),
) -> None:
    """Reject a task and move it to a safe non-running state."""
    if not reason:
        raise typer.BadParameter("reason must not be empty")
    _ = checkpoint_dir, task_registry
    manager = TaskLifecycleManager(lifecycle_store)
    try:
        current_state = manager.get_task_state(task_id)
    except KeyError as exc:
        raise typer.BadParameter(f"Unknown task: {task_id}") from exc

    if current_state in {TaskState.completed, TaskState.failed, TaskState.stopped}:
        console.print(f"Task '{task_id}' is already terminal ({current_state.value}).")
        return

    target_state = (
        TaskState.paused
        if current_state in {TaskState.running, TaskState.paused, TaskState.suspended}
        else TaskState.stopped
    )
    try:
        manager.transition(task_id, target_state)
    except InvalidTransitionError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"Task '{task_id}' rejected ({reason}) and moved to {target_state.value}.")


@app.command("pause")
def pause(
    task_id: str = typer.Argument(..., help="Task ID to pause."),
    lifecycle_store: Path = typer.Option(
        Path("runtime/lifecycle.json"),
        "--lifecycle-store",
        help="Path to persisted lifecycle store.",
    ),
    checkpoint_dir: Path = typer.Option(
        Path("runtime/checkpoints"),
        "--checkpoint-dir",
        help="Directory containing task checkpoints.",
    ),
    task_registry: Path = typer.Option(
        Path("runtime/tasks.json"),
        "--task-registry",
        help="Path to task metadata registry.",
    ),
) -> None:
    """Pause a running task after saving a checkpoint marker."""
    _ = task_registry
    manager = TaskLifecycleManager(lifecycle_store)
    try:
        current_state = manager.get_task_state(task_id)
    except KeyError as exc:
        raise typer.BadParameter(f"Unknown task: {task_id}") from exc

    if current_state != TaskState.running:
        raise typer.BadParameter(f"Task '{task_id}' must be running to pause (current: {current_state.value}).")

    checkpoint_manager = CheckpointManager(checkpoint_dir)
    checkpoint_manager.save_step_completion(task_id, "manual_pause")
    manager.transition(task_id, TaskState.paused)
    console.print(f"Task '{task_id}' paused.")


@app.command("stop")
def stop(
    task_id: str = typer.Argument(..., help="Task ID to stop immediately."),
    lifecycle_store: Path = typer.Option(
        Path("runtime/lifecycle.json"),
        "--lifecycle-store",
        help="Path to persisted lifecycle store.",
    ),
    checkpoint_dir: Path = typer.Option(
        Path("runtime/checkpoints"),
        "--checkpoint-dir",
        help="Directory where checkpoints are stored.",
    ),
    audit_log: Path = typer.Option(
        Path("runtime/audit/audit.log"),
        "--audit-log",
        help="Path to append-only audit log file.",
    ),
    reason: str = typer.Option(
        "owner_kill_switch",
        "--reason",
        help="Stop reason recorded in event payload and checkpoint step id.",
    ),
    artifacts: list[str] | None = typer.Option(
        None,
        "--artifact",
        help="Partial artifact path to preserve. Can be repeated.",
    ),
) -> None:
    """Stop a task immediately and preserve checkpoint and audit evidence."""
    bus = EventBus()
    lifecycle_manager = TaskLifecycleManager(lifecycle_store, event_bus=bus)
    checkpoint_manager = CheckpointManager(
        checkpoint_dir,
        event_bus=bus,
        lifecycle_manager=lifecycle_manager,
    )
    audit_writer = AuditLogWriter(audit_log)
    controller = SafetyController(
        event_bus=bus,
        lifecycle_manager=lifecycle_manager,
        checkpoint_manager=checkpoint_manager,
        audit_log_writer=audit_writer,
    )

    try:
        previous_state = lifecycle_manager.get_task_state(task_id)
    except KeyError as exc:
        raise typer.BadParameter(f"Unknown task: {task_id}") from exc

    controller.stop_task(
        task_id=task_id,
        reason=reason,
        artifacts=artifacts,
    )
    current_state = lifecycle_manager.get_task_state(task_id)
    if previous_state == current_state and current_state != TaskState.stopped:
        console.print(
            f"Task '{task_id}' is already terminal ({current_state.value}); no state change was applied."
        )
        return

    checkpoint_path = checkpoint_manager.checkpoint_file_for_task(task_id)
    console.print(f"Task '{task_id}' is now '{current_state.value}'.")
    console.print(f"Checkpoint: {checkpoint_path}")
    console.print(f"Audit log: {audit_log}")


@app.command("resume")
def resume(
    task_id: str = typer.Argument(..., help="Task ID to resume from checkpoint."),
    lifecycle_store: Path = typer.Option(
        Path("runtime/lifecycle.json"),
        "--lifecycle-store",
        help="Path to persisted lifecycle store.",
    ),
    checkpoint_dir: Path = typer.Option(
        Path("runtime/checkpoints"),
        "--checkpoint-dir",
        help="Directory containing task checkpoint files.",
    ),
    task_registry: Path = typer.Option(
        Path("runtime/tasks.json"),
        "--task-registry",
        help="Path to task metadata registry.",
    ),
) -> None:
    """Resume a task from its latest checkpoint."""
    _ = task_registry
    manager = CheckpointManager(checkpoint_dir)
    try:
        resume_info = manager.resume_task(task_id)
    except CheckpointCorruptedError as exc:
        raise typer.BadParameter(str(exc)) from exc

    if resume_info is None:
        console.print(f"No checkpoint found for task '{task_id}'. Starting from the beginning.")
    else:
        if resume_info.resume_after_step_id is None:
            console.print(f"Resuming task '{task_id}' from the beginning.")
        else:
            console.print(
                f"Resuming task '{task_id}' after step '{resume_info.resume_after_step_id}'."
            )

    lifecycle_manager = TaskLifecycleManager(lifecycle_store)
    try:
        current_state = lifecycle_manager.get_task_state(task_id)
    except KeyError:
        return
    if current_state in {TaskState.completed, TaskState.failed, TaskState.stopped, TaskState.running}:
        return
    try:
        lifecycle_manager.transition(task_id, TaskState.running)
    except InvalidTransitionError:
        return
    console.print(f"Task '{task_id}' moved to running.")


@app.command("feedback")
def feedback(
    task_id: str = typer.Argument(..., help="Task ID for feedback."),
    rating: int = typer.Option(..., "--rating", min=1, max=5, help="Rating score (1-5)."),
    comment: str = typer.Option(..., "--comment", help="Feedback comment."),
    lifecycle_store: Path = typer.Option(
        Path("runtime/lifecycle.json"),
        "--lifecycle-store",
        help="Path to persisted lifecycle store.",
    ),
    task_registry: Path = typer.Option(
        Path("runtime/tasks.json"),
        "--task-registry",
        help="Path to task metadata registry.",
    ),
    memory_file: Path = typer.Option(
        Path("runtime/memory/episodic_feedback.jsonl"),
        "--memory-file",
        help="Path to episodic memory feedback JSONL file.",
    ),
) -> None:
    """Record owner feedback into episodic memory."""
    manager = TaskLifecycleManager(lifecycle_store)
    try:
        task_state = manager.get_task_state(task_id)
    except KeyError as exc:
        raise typer.BadParameter(f"Unknown task: {task_id}") from exc

    registry = _load_task_registry(task_registry)
    goal = _task_goal(registry, task_id) or ""
    memory = EpisodicMemory(
        task_id=task_id,
        goal=goal,
        execution_state=task_state,
        metadata={
            "feedback": {
                "rating": rating,
                "comment": comment,
                "recorded_at": _timestamp_text(_utc_now()),
            }
        },
    )

    memory_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "task_id": task_id,
        "rating": rating,
        "comment": comment,
        "recorded_at": _timestamp_text(_utc_now()),
        "episodic_memory": memory.model_dump(mode="json"),
    }
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with memory_file.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.write("\n")
    console.print(f"Feedback recorded for task '{task_id}'.")


@app.command("log")
def log(
    task_id: str = typer.Argument(..., help="Task ID to show audit log for."),
    audit_log: Path = typer.Option(
        Path("runtime/audit/audit.log"),
        "--audit-log",
        help="Path to append-only audit log file.",
    ),
    event_type: str | None = typer.Option(
        None,
        "--event-type",
        help="Optional event type filter.",
    ),
) -> None:
    """Show chronological audit log entries for a task."""
    selected_event_type: EventType | str | None = event_type
    if event_type is not None:
        try:
            selected_event_type = EventType(event_type)
        except ValueError as exc:
            raise typer.BadParameter(f"Unknown event type: {event_type}") from exc

    reader = AuditLogReader(audit_log)
    events = reader.iter_events(task_id=task_id, event_type=selected_event_type)
    if not events:
        console.print(f"No audit log entries for task '{task_id}'.")
        return

    for event in sorted(events, key=lambda item: item.timestamp):
        payload_text = json.dumps(event.payload, ensure_ascii=False, sort_keys=True)
        timestamp_text = _timestamp_text(event.timestamp)
        console.print(
            f"{timestamp_text}\t{event.task_id}\t{event.event_type.value}\t{payload_text}"
        )


@app.command("health")
def health(
    config_file: Path | None = typer.Option(
        None,
        "--config-file",
        "-c",
        help="Path to agent_config.yaml. Defaults to repository config path.",
    ),
) -> None:
    """Show UP/DOWN health status for core and MVP tools."""
    checks: list[tuple[str, bool, str]] = []

    try:
        _ = load_config(config_file)
        checks.append(("config_loader", True, "config schema validated"))
    except Exception as exc:
        checks.append(("config_loader", False, f"config load failed: {exc}"))

    checks.append(("event_bus", True, "in-process event bus available"))
    checks.append(("audit_log", True, "append-only audit log support available"))
    checks.append(("web_search", False, "tool not implemented yet"))
    checks.append(("llm_analyze", False, "tool not implemented yet"))
    checks.append(("report_render", False, "tool not implemented yet"))

    for name, is_up, detail in checks:
        status_text = "UP" if is_up else "DOWN"
        console.print(f"{name}\t{status_text}\t{detail}")


@memory_app.command("list")
def memory_list(
    semantic_store: Path = typer.Option(
        Path("runtime/memory/semantic.json"),
        "--semantic-store",
        help="Path to persisted semantic memory store.",
    ),
    category: str | None = typer.Option(
        None,
        "--category",
        help="Optional category filter.",
    ),
    active_only: bool = typer.Option(
        True,
        "--active-only/--all",
        help="Show only active semantic memories.",
    ),
) -> None:
    """List semantic memories grouped by category."""
    store = SemanticMemoryStore(semantic_store)
    snapshot = store.load()
    rows: list[tuple[str, str, str, float, float, float, int]] = []

    for pattern_id in sorted(snapshot.records):
        record = snapshot.records[pattern_id]
        if active_only and not record.active:
            continue
        if category is not None and record.memory.category.casefold() != category.casefold():
            continue
        metadata = record.memory.metadata
        confidence = 0.0
        raw_confidence = metadata.get("confidence", 0.0)
        if isinstance(raw_confidence, (int, float)):
            confidence = float(raw_confidence)
        score = confidence + record.memory.success_rate
        source_episodes_raw = metadata.get("source_episodes", [])
        source_episode_count = (
            len(cast(list[object], source_episodes_raw))
            if isinstance(source_episodes_raw, list)
            else 0
        )
        rows.append(
            (
                record.memory.category,
                record.memory.pattern_id,
                "YES" if record.active else "NO",
                score,
                confidence,
                record.memory.success_rate,
                source_episode_count,
            )
        )

    if not rows:
        console.print("No semantic memories found.")
        return

    rows.sort(key=lambda item: (item[0], -item[3], item[1]))

    table = Table(title="Semantic Memories")
    table.add_column("Category")
    table.add_column("Pattern ID")
    table.add_column("Active")
    table.add_column("Score", justify="right")
    table.add_column("Confidence", justify="right")
    table.add_column("Success", justify="right")
    table.add_column("Source Episodes", justify="right")

    for row in rows:
        table.add_row(
            row[0],
            row[1],
            row[2],
            f"{row[3]:.2f}",
            f"{row[4]:.2f}",
            f"{row[5]:.2f}",
            str(row[6]),
        )

    console.print(table)


@memory_app.command("show")
def memory_show(
    pattern_id: str = typer.Argument(..., help="Semantic pattern ID."),
    semantic_store: Path = typer.Option(
        Path("runtime/memory/semantic.json"),
        "--semantic-store",
        help="Path to persisted semantic memory store.",
    ),
    episodic_store: Path = typer.Option(
        Path("runtime/memory/episodic.json"),
        "--episodic-store",
        help="Path to persisted episodic memory store.",
    ),
) -> None:
    """Show a semantic memory and its linked source episodes."""
    store = SemanticMemoryStore(semantic_store)
    try:
        record = store.get(pattern_id)
    except KeyError as exc:
        raise typer.BadParameter(f"Unknown semantic pattern: {pattern_id}") from exc

    memory = record.memory
    metadata = memory.metadata
    confidence = 0.0
    raw_confidence = metadata.get("confidence", 0.0)
    if isinstance(raw_confidence, (int, float)):
        confidence = float(raw_confidence)
    source_episodes_raw = metadata.get("source_episodes", [])
    source_episode_ids = (
        [
            item
            for item in cast(list[object], source_episodes_raw)
            if isinstance(item, str)
        ]
        if isinstance(source_episodes_raw, list)
        else []
    )

    table = Table(title=f"Semantic Memory: {pattern_id}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Category", memory.category)
    table.add_row("Active", "YES" if record.active else "NO")
    table.add_row("Pattern", memory.pattern)
    table.add_row("Conditions", ", ".join(memory.conditions) or "-")
    table.add_row("Actions", ", ".join(memory.actions) or "-")
    table.add_row("Success Rate", f"{memory.success_rate:.2f}")
    table.add_row("Confidence", f"{confidence:.2f}")
    table.add_row("Source Episodes", ", ".join(source_episode_ids) or "-")
    table.add_row("Metadata", json.dumps(metadata, ensure_ascii=False, sort_keys=True))
    console.print(table)

    episodic_lookup = EpisodicMemoryStore(episodic_store)
    linked_episodes: list[EpisodicMemory] = []
    for episode_id in source_episode_ids:
        try:
            linked_episodes.append(episodic_lookup.read(episode_id))
        except KeyError:
            continue

    if not linked_episodes:
        return

    linked_table = Table(title="Linked Episodic Memories")
    linked_table.add_column("Task ID")
    linked_table.add_column("Goal")
    linked_table.add_column("Execution State")
    for episode in linked_episodes:
        linked_table.add_row(episode.task_id, episode.goal, episode.execution_state.value)
    console.print(linked_table)


@memory_app.command("sync")
def memory_sync(
    episodic_store: Path = typer.Option(
        Path("runtime/memory/episodic.json"),
        "--episodic-store",
        help="Path to persisted episodic memory store.",
    ),
    semantic_store: Path = typer.Option(
        Path("runtime/memory/semantic.json"),
        "--semantic-store",
        help="Path to persisted semantic memory store.",
    ),
    threshold: int | None = typer.Option(
        None,
        "--threshold",
        min=1,
        help="Episode count threshold for extraction. Defaults to config.memory.hot_episodes.",
    ),
) -> None:
    """Extract semantic memories from episodic memories."""
    extractor = MemoryPatternExtractor.from_paths(
        episodic_store,
        semantic_store,
        threshold=threshold,
    )
    created = extractor.sync_all()
    if not created:
        console.print("No semantic memories extracted.")
        return

    for record in created:
        console.print(f"Extracted {record.memory.pattern_id} from {record.memory.category}.")


@memory_app.command("import")
def memory_import(
    source: Path = typer.Argument(..., help="JSON/YAML file containing episodic memories."),
    episodic_store: Path = typer.Option(
        Path("runtime/memory/episodic.json"),
        "--episodic-store",
        help="Path to persisted episodic memory store.",
    ),
) -> None:
    """Import episodic memories from a JSON or YAML file."""
    records = _load_episodic_memory_batch(source)
    store = EpisodicMemoryStore(episodic_store)
    imported_count = 0
    for memory in records:
        store.create(memory)
        imported_count += 1
    console.print(f"Imported {imported_count} episodic memories.")


@queue_app.command("run")
def queue_run(
    lifecycle_store: Path = typer.Option(
        Path("runtime/lifecycle.json"),
        "--lifecycle-store",
        help="Path to persisted lifecycle store.",
    ),
) -> None:
    """Start the next pending task from the queue when the runner is idle."""
    runner = QueueRunner(lifecycle_store)
    result = runner.run_once()

    if result.started_task_id is None:
        console.print("Queue runner idle.")
    else:
        console.print(f"Started task '{result.started_task_id}' from queue.")
    console.print(f"Running task: {result.after_running_task_id or '-'}")
    console.print(f"Pending queue: {', '.join(result.after_pending_queue) or '-'}")


@review_app.command("assess")
def review_assess(
    task_id: str = typer.Argument(..., help="Task ID to review."),
    memory_file: Path = typer.Option(
        Path("runtime/memory/episodic_feedback.jsonl"),
        "--memory-file",
        help="Path to episodic feedback JSONL file.",
    ),
) -> None:
    """Assess the latest episodic feedback record for a task."""
    report = ReviewEngine(memory_file).review(task_id)
    if report is None:
        raise typer.BadParameter(f"Unknown or unreviewable task: {task_id}")

    table = Table(title=f"Review: {task_id}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Verdict", report.verdict)
    table.add_row("Overall", f"{report.overall_score:.2f}")
    table.add_row("Goal Achievement", f"{report.goal_achievement:.2f}")
    table.add_row("Reliability", f"{report.information_reliability:.2f}")
    table.add_row("Clarity", f"{report.structure_clarity:.2f}")
    table.add_row("Efficiency", f"{report.efficiency:.2f}")
    table.add_row("Checklist", report.checklist_summary())
    table.add_row("Reasoning", report.reasoning)
    console.print(table)


@review_app.command("evaluate")
def review_evaluate(
    task_id: str = typer.Argument(..., help="Task ID to self-evaluate."),
    memory_file: Path = typer.Option(
        Path("runtime/memory/episodic_feedback.jsonl"),
        "--memory-file",
        help="Path to episodic feedback JSONL file.",
    ),
) -> None:
    """Generate a self-evaluation report for a task."""
    report = SelfEvaluationEngine(memory_file).evaluate(task_id)
    if report is None:
        raise typer.BadParameter(f"Unknown or unevaluable task: {task_id}")

    table = Table(title=f"Self Evaluation: {task_id}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Overall", f"{report.overall_score:.2f}")
    table.add_row("Goal Achievement", f"{report.goal_achievement:.2f}")
    table.add_row("Reliability", f"{report.information_reliability:.2f}")
    table.add_row("Clarity", f"{report.structure_clarity:.2f}")
    table.add_row("Efficiency", f"{report.efficiency:.2f}")
    table.add_row("Checklist", report.checklist_summary())
    table.add_row("Fact Check", report.fact_check_summary())
    table.add_row("Reasoning", report.reasoning)
    console.print(table)
    console.print(json.dumps(report.to_self_evaluation(), ensure_ascii=False, sort_keys=True))


@review_app.command("improve")
def review_improve(
    task_id: str = typer.Argument(..., help="Task ID to improve."),
    memory_file: Path = typer.Option(
        Path("runtime/memory/episodic_feedback.jsonl"),
        "--memory-file",
        help="Path to episodic feedback JSONL file.",
    ),
    config_file: Path | None = typer.Option(
        None,
        "--config-file",
        "-c",
        help="Path to agent_config.yaml. Defaults to repository config path.",
    ),
    artifact_dir: Path | None = typer.Option(
        None,
        "--artifact-dir",
        help="Directory containing versioned result artifacts.",
    ),
) -> None:
    """Run the minimal improvement loop for a task."""
    report = ImprovementLoopEngine(memory_file, config_file=config_file).improve(
        task_id,
        artifact_dir=artifact_dir,
    )
    if report is None:
        raise typer.BadParameter(f"Unknown or unimprovable task: {task_id}")

    table = Table(title=f"Improvement Loop: {task_id}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Triggered", str(report.triggered))
    table.add_row("Status", report.status)
    table.add_row("Iterations", str(report.iterations))
    table.add_row("Start Score", f"{report.start_score:.2f}")
    table.add_row("Final Score", f"{report.final_score:.2f}")
    table.add_row("Previous Score", "-" if report.previous_score is None else f"{report.previous_score:.2f}")
    table.add_row("Lessons", "; ".join(report.lessons) or "-")
    table.add_row("Rollback Target", report.rollback_target or "-")
    table.add_row("Reasoning", report.reasoning)
    console.print(table)
    console.print(json.dumps(report.to_improvement_loop(), ensure_ascii=False, sort_keys=True))


@trust_app.command("list")
def trust_list(
    feedback_file: Path = typer.Option(
        Path("runtime/memory/episodic_feedback.jsonl"),
        "--feedback-file",
        help="Path to episodic feedback JSONL file.",
    ),
    db_file: Path = typer.Option(
        Path("runtime/trust/trust_scores.sqlite3"),
        "--db-file",
        help="Path to persisted trust score SQLite database.",
    ),
    window_size: int = typer.Option(
        20,
        "--window-size",
        min=1,
        help="Number of recent tasks per category used for scoring.",
    ),
    rating_weight: float = typer.Option(
        1.0,
        "--rating-weight",
        min=0.0,
        max=1.0,
        help="Weight applied to owner ratings when scoring is calculated.",
    ),
    rating_scale_max: float = typer.Option(
        5.0,
        "--rating-scale-max",
        min=0.1,
        help="Maximum owner rating used for normalization.",
    ),
) -> None:
    """Show trust scores per category and persist them to SQLite."""
    store = TrustScoreStore(db_file)
    scores: list[TrustScore] = []
    if feedback_file.exists():
        scores = TrustScoreCalculator(
            feedback_file,
            window_size=window_size,
            rating_weight=rating_weight,
            rating_scale_max=rating_scale_max,
        ).calculate()
        if scores:
            store.replace_all(scores)
    if not scores:
        scores = store.load_all()

    if not scores:
        console.print("No trust scores found.")
        return

    table = Table(title="Trust Scores")
    table.add_column("Category")
    table.add_column("Score", justify="right")
    table.add_column("Tasks", justify="right")
    table.add_column("Success", justify="right")
    table.add_column("Avg Rating", justify="right")
    table.add_column("Last Updated")

    for score in scores:
        table.add_row(
            score.category,
            f"{score.score:.2f}",
            str(score.task_count),
            str(score.success_count),
            "-" if score.avg_owner_rating is None else f"{score.avg_owner_rating:.2f}",
            _timestamp_text(score.last_updated),
        )

    console.print(table)


@tool_app.command("list")
def tool_list(
    plugins_file: Path = typer.Option(
        Path("runtime/tools/plugins.json"),
        "--plugins-file",
        help="Path to persisted plugin manifest.",
    ),
) -> None:
    """Show registered tools with name, risk level, and UP/DOWN health."""
    registry = ToolRegistry()
    risk_levels: dict[str, str] = {}
    failed_plugins: list[tuple[str, str, str]] = []

    for tool in _builtin_tools():
        if registry.is_registered(tool.name):
            continue
        registry.register(tool, require_approval=False)
        risk_levels[tool.name] = tool.risk_level.value

    manifest_entries = _load_tool_plugin_manifest(plugins_file)
    for entry in manifest_entries:
        plugin_name = entry["name"]
        plugin_path = Path(entry["plugin_path"])
        try:
            plugin_tool = _load_tool_from_plugin(plugin_path)
            if not registry.is_registered(plugin_tool.name):
                registry.register(plugin_tool, require_approval=False)
            risk_levels[plugin_tool.name] = plugin_tool.risk_level.value
        except Exception as exc:
            failed_plugins.append((plugin_name, "unknown", f"DOWN ({exc})"))

    statuses = registry.health_check_all()
    rows: list[tuple[str, str, str]] = []
    for tool_name in registry.list_tools():
        risk = risk_levels.get(tool_name, "unknown")
        status = "UP" if statuses.get(tool_name, False) else "DOWN"
        rows.append((tool_name, risk, status))

    for plugin_name, risk, status in failed_plugins:
        rows.append((plugin_name, risk, status))

    if not rows:
        console.print("No tools found.")
        return

    for tool_name, risk, status in sorted(rows, key=lambda item: item[0]):
        console.print(f"{tool_name}\t{risk}\t{status}")


@tool_app.command("add")
def tool_add(
    plugin_path: Path = typer.Argument(..., help="Path to plugin Python file."),
    plugins_file: Path = typer.Option(
        Path("runtime/tools/plugins.json"),
        "--plugins-file",
        help="Path to persisted plugin manifest.",
    ),
    task_id: str = typer.Option(
        "tool_management",
        "--task-id",
        help="Task ID for escalation tracking.",
    ),
    auto_approve: bool = typer.Option(
        True,
        "--auto-approve/--no-auto-approve",
        help="Auto-approve L3 request in the same command.",
    ),
) -> None:
    """Load plugin, request L3 approval, and register tool."""
    try:
        tool = _load_tool_from_plugin(plugin_path)
    except Exception as exc:
        raise typer.BadParameter(str(exc)) from exc

    event_bus = EventBus()
    escalation_engine = EscalationEngine(event_bus=event_bus)
    registry = ToolRegistry(escalation_engine=escalation_engine)
    registration = registry.register(
        tool,
        require_approval=True,
        auto_approve=False,
        task_id=task_id,
    )
    request_id = registration.request_id
    if request_id is None:
        raise typer.BadParameter("Failed to create L3 approval request.")

    console.print(f"L3 approval requested for tool '{tool.name}': {request_id}")
    if not auto_approve:
        console.print("Waiting for approval. Tool is not registered yet.")
        return

    registry.approve_registration(tool.name, request_id)
    _append_tool_plugin_manifest(plugins_file, tool.name, plugin_path)
    console.print(f"L3 approval granted: {request_id}")
    console.print(f"Tool '{tool.name}' registered.")


@config_app.command("show")
def config_show(
    config_file: Path | None = typer.Option(
        None,
        "--config-file",
        "-c",
        help="Path to agent_config.yaml. Defaults to repository config path.",
    ),
    set_values: list[str] | None = typer.Option(
        None,
        "--set",
        help="Runtime override in dot.path=value form. Can be repeated.",
    ),
) -> None:
    """Load, validate, and print the effective agent configuration."""
    try:
        config = load_config(config_file)
        overrides = parse_runtime_overrides(set_values)
        config = apply_runtime_overrides(config, overrides)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    console.print(config.model_dump_json(indent=2))


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Configuration key path (dot notation)."),
    value: str = typer.Argument(..., help="YAML-compatible value (e.g. 9, true, \"text\")."),
    config_file: Path | None = typer.Option(
        None,
        "--config-file",
        "-c",
        help="Path to agent_config.yaml. Defaults to repository config path.",
    ),
) -> None:
    """Persist a configuration value, rejecting unsafe safety.* relaxations."""
    target_path = config_file.expanduser() if config_file is not None else DEFAULT_CONFIG_PATH

    if not key:
        raise typer.BadParameter("key must not be empty")

    base_config: AgentConfig
    try:
        base_config = load_config(target_path)
        parsed_override = parse_runtime_overrides([f"{key}={value}"])
        updated = apply_runtime_overrides(base_config, parsed_override)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    target_path.parent.mkdir(parents=True, exist_ok=True)
    dumped = yaml.safe_dump(
        updated.model_dump(mode="python"),
        allow_unicode=True,
        sort_keys=False,
    )
    target_path.write_text(dumped, encoding="utf-8")
    console.print(f"Updated {key} in {target_path}.")
