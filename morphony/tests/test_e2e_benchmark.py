from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

from typer.testing import CliRunner

from morphony.cli import app
from morphony.events import AuditLogWriter, EventBus
from morphony.lifecycle import CheckpointManager, TaskLifecycleManager
from morphony.models import EpisodicMemory, EscalationLevel, TaskState
from morphony.observability import ObservabilityEngine
from morphony.review import SelfEvaluationEngine
from morphony.safety import BudgetController, EscalationEngine
from morphony.tools import ToolExecutionRunner, ToolRegistry


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _slugify(text: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in text).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "query"


def _write_task_registry(path: Path, task_id: str, goal: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                task_id: {
                    "goal": goal,
                    "created_at": _utc_text(datetime.now(UTC)),
                }
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_feedback_record(path: Path, memory: EpisodicMemory) -> None:
    payload = {
        "task_id": memory.task_id,
        "rating": 5,
        "comment": "e2e scenario completed",
        "recorded_at": _utc_text(datetime.now(UTC)),
        "episodic_memory": memory.model_dump(mode="json"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


@dataclass(frozen=True, slots=True)
class E2ERuntime:
    lifecycle_store: Path
    checkpoint_dir: Path
    task_registry: Path
    memory_file: Path
    audit_log: Path
    summary_dir: Path
    lifecycle: TaskLifecycleManager
    checkpoint: CheckpointManager
    budget: BudgetController
    escalation: EscalationEngine
    registry: ToolRegistry
    runner: ToolExecutionRunner
    observability: ObservabilityEngine


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    task_id: str
    goal: str
    score: float
    duration_seconds: float
    total_cost_usd: float
    summary_path: Path | None
    cli_output: str


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    name: str
    query: str
    delay_seconds: float
    evidence_count: int
    rating: int


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    case: BenchmarkCase
    score: float
    duration_seconds: float
    total_cost_usd: float


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    results: list[BenchmarkResult]

    @property
    def average_score(self) -> float:
        if not self.results:
            return 0.0
        return round(sum(item.score for item in self.results) / len(self.results), 2)

    @property
    def total_cost_usd(self) -> float:
        return round(sum(item.total_cost_usd for item in self.results), 4)

    @property
    def total_duration_seconds(self) -> float:
        return round(sum(item.duration_seconds for item in self.results), 4)

    @property
    def score_delta(self) -> float:
        if len(self.results) < 2:
            return 0.0
        return round(self.results[-1].score - self.results[0].score, 2)

    @property
    def duration_delta(self) -> float:
        if len(self.results) < 2:
            return 0.0
        return round(self.results[-1].duration_seconds - self.results[0].duration_seconds, 4)


class _MockSearchTool:
    name = "mock_web_search"
    description = "Mock search tool for E2E coverage."
    risk_level = EscalationLevel.L1
    cost_per_call = Decimal("0")
    is_reversible = True

    def __init__(self, delay_seconds: float = 0.0) -> None:
        self._delay_seconds = delay_seconds

    def validate(self, *args: object, **kwargs: object) -> bool:
        query = kwargs.get("query")
        return isinstance(query, str) and bool(query.strip())

    def execute(self, *args: object, **kwargs: object) -> list[dict[str, str]]:
        query = kwargs.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        raw_limit = kwargs.get("limit", 3)
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
            raise TypeError("limit must be an integer")
        time.sleep(self._delay_seconds)
        slug = _slugify(query)
        return [
            {
                "title": f"{query} result {index}",
                "url": f"https://example.com/{slug}/{index}",
                "snippet": f"{query} snippet {index}",
            }
            for index in range(1, max(1, raw_limit) + 1)
        ]

    def health_check(self) -> bool:
        return True


class _MockFetchTool:
    name = "mock_web_fetch"
    description = "Mock fetch tool for E2E coverage."
    risk_level = EscalationLevel.L1
    cost_per_call = Decimal("0")
    is_reversible = True

    def __init__(self, delay_seconds: float = 0.0) -> None:
        self._delay_seconds = delay_seconds

    def validate(self, *args: object, **kwargs: object) -> bool:
        url = kwargs.get("url")
        return isinstance(url, str) and bool(url.strip())

    def execute(self, *args: object, **kwargs: object) -> dict[str, str]:
        url = kwargs.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("url must be a non-empty string")
        time.sleep(self._delay_seconds)
        return {
            "url": url.strip(),
            "text": f"Fetched content for {url.strip()}",
        }

    def health_check(self) -> bool:
        return True


class _MockAnalyzeTool:
    name = "mock_llm_analyze"
    description = "Mock analyze tool for E2E coverage."
    risk_level = EscalationLevel.L1
    cost_per_call = Decimal("0")
    is_reversible = True

    def __init__(self, delay_seconds: float = 0.0) -> None:
        self._delay_seconds = delay_seconds

    def validate(self, *args: object, **kwargs: object) -> bool:
        text = kwargs.get("text")
        instruction = kwargs.get("instruction")
        return isinstance(text, str) and bool(text.strip()) and isinstance(instruction, str) and bool(instruction.strip())

    def execute(self, *args: object, **kwargs: object) -> dict[str, object]:
        text = kwargs.get("text")
        instruction = kwargs.get("instruction")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("instruction must be a non-empty string")
        time.sleep(self._delay_seconds)
        summary = text.strip().split(".")[0].strip()
        return {
            "instruction": instruction.strip(),
            "summary": summary or text.strip(),
            "key_points": [summary or text.strip()],
            "estimated_tokens": max(1, (len(text) + len(instruction)) // 4),
            "estimated_cost_usd": 0.0,
        }

    def health_check(self) -> bool:
        return True


class _MockReportRenderTool:
    name = "mock_report_render"
    description = "Mock report renderer for E2E coverage."
    risk_level = EscalationLevel.L1
    cost_per_call = Decimal("0")
    is_reversible = True

    def __init__(self, delay_seconds: float = 0.0) -> None:
        self._delay_seconds = delay_seconds

    def validate(self, *args: object, **kwargs: object) -> bool:
        summary = kwargs.get("summary")
        return isinstance(summary, str) and bool(summary.strip())

    def execute(self, *args: object, **kwargs: object) -> str:
        summary = kwargs.get("summary")
        details = kwargs.get("details", [])
        sources = kwargs.get("sources", [])
        metadata = kwargs.get("metadata", {})
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("summary must be a non-empty string")
        time.sleep(self._delay_seconds)
        detail_lines = [f"- {item}" for item in details if isinstance(item, str) and item.strip()]
        source_lines = []
        for item in sources:
            if isinstance(item, dict):
                title = item.get("title")
                url = item.get("url")
                if isinstance(title, str) and isinstance(url, str):
                    source_lines.append(f"- [{title}]({url})")
        metadata_json = json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True)
        return "\n".join(
            [
                "## Summary",
                summary.strip(),
                "",
                "## Details",
                *(detail_lines or ["- (none)"]),
                "",
                "## Sources",
                *(source_lines or ["- (none)"]),
                "",
                "## Metadata",
                "```json",
                metadata_json,
                "```",
            ]
        )

    def health_check(self) -> bool:
        return True


def _build_runtime(tmp_path: Path) -> E2ERuntime:
    lifecycle_store = tmp_path / "runtime" / "lifecycle.json"
    checkpoint_dir = tmp_path / "runtime" / "checkpoints"
    task_registry = tmp_path / "runtime" / "tasks.json"
    memory_file = tmp_path / "runtime" / "memory" / "episodic_feedback.jsonl"
    audit_log = tmp_path / "runtime" / "audit" / "audit.log"
    summary_dir = tmp_path / "runtime" / "summaries"

    bus = EventBus()
    audit_writer = AuditLogWriter(audit_log)
    bus.subscribe_all(audit_writer.append)

    lifecycle = TaskLifecycleManager(lifecycle_store, event_bus=bus)
    checkpoint = CheckpointManager(
        checkpoint_dir,
        event_bus=bus,
        lifecycle_manager=lifecycle,
    )
    budget = BudgetController(event_bus=bus, lifecycle_manager=lifecycle)
    escalation = EscalationEngine(
        event_bus=bus,
        lifecycle_manager=lifecycle,
        checkpoint_manager=checkpoint,
    )
    registry = ToolRegistry()
    runner = ToolExecutionRunner(
        registry,
        event_bus=bus,
        audit_log_writer=audit_writer,
        budget_controller=budget,
        escalation_engine=escalation,
    )
    observability = ObservabilityEngine(
        lifecycle_store=lifecycle_store,
        checkpoint_dir=checkpoint_dir,
        audit_log=audit_log,
        memory_file=memory_file,
        summary_dir=summary_dir,
    )
    return E2ERuntime(
        lifecycle_store=lifecycle_store,
        checkpoint_dir=checkpoint_dir,
        task_registry=task_registry,
        memory_file=memory_file,
        audit_log=audit_log,
        summary_dir=summary_dir,
        lifecycle=lifecycle,
        checkpoint=checkpoint,
        budget=budget,
        escalation=escalation,
        registry=registry,
        runner=runner,
        observability=observability,
    )


def _register_mock_tools(runtime: E2ERuntime, *, delay_seconds: float = 0.0) -> None:
    runtime.registry.register(_MockSearchTool(delay_seconds=delay_seconds), require_approval=False)
    runtime.registry.register(_MockFetchTool(delay_seconds=delay_seconds), require_approval=False)
    runtime.registry.register(_MockAnalyzeTool(delay_seconds=delay_seconds), require_approval=False)
    runtime.registry.register(_MockReportRenderTool(delay_seconds=delay_seconds), require_approval=False)


def _completed_memory(
    task_id: str,
    goal: str,
    *,
    result: object,
    evidence_count: int,
    rating: int,
    total_cost: float,
    total_duration_minutes: float,
) -> EpisodicMemory:
    evidence = [f"{task_id}-evidence-{index}" for index in range(1, evidence_count + 1)]
    return EpisodicMemory(
        task_id=task_id,
        goal=goal,
        plan=["search", "fetch", "analyze", "render"],
        steps=[
            {"step": "search", "status": "done"},
            {"step": "fetch", "status": "done"},
            {"step": "analyze", "status": "done"},
            {"step": "render", "status": "done"},
        ],
        result=result,
        execution_state=TaskState.completed,
        metadata={
            "category": "research",
            "feedback": {
                "rating": rating,
                "comment": "e2e scenario completed",
                "recorded_at": _utc_text(datetime.now(UTC)),
            },
            "evidence": evidence,
            "sources": [f"{task_id}-source-{index}" for index in range(1, max(1, evidence_count) + 1)],
            "total_cost": total_cost,
            "total_duration_minutes": total_duration_minutes,
        },
    )


def _run_mock_research_flow(
    tmp_path: Path,
    *,
    case_name: str,
    query: str,
    delay_seconds: float,
    evidence_count: int,
    rating: int,
    use_cli_status: bool,
) -> ScenarioResult:
    runtime = _build_runtime(tmp_path / case_name)
    _register_mock_tools(runtime, delay_seconds=delay_seconds)

    task_id = f"task-{case_name}"
    goal = f"research {query}"
    _write_task_registry(runtime.task_registry, task_id, goal)
    runtime.lifecycle.submit_task(task_id)

    started_at = time.perf_counter()
    search_result = runtime.runner.execute(
        task_id,
        "mock_web_search",
        tool_input={"query": query, "limit": 3},
        now=datetime(2026, 3, 27, 12, 0, tzinfo=UTC),
    )
    search_payload = cast(list[dict[str, str]], search_result.output)
    runtime.checkpoint.save_step_completion(
        task_id,
        "search",
        artifacts=[f"{case_name}-search.json"],
        budget_delta={"cost_usd": search_result.cost_usd},
    )

    fetch_result = runtime.runner.execute(
        task_id,
        "mock_web_fetch",
        tool_input={"url": search_payload[0]["url"]},
        now=datetime(2026, 3, 27, 12, 1, tzinfo=UTC),
    )
    fetch_payload = cast(dict[str, str], fetch_result.output)
    runtime.checkpoint.save_step_completion(
        task_id,
        "fetch",
        artifacts=[f"{case_name}-fetch.txt"],
        budget_delta={"cost_usd": fetch_result.cost_usd},
    )

    analyze_result = runtime.runner.execute(
        task_id,
        "mock_llm_analyze",
        tool_input={
            "text": fetch_payload["text"],
            "instruction": "summarize",
        },
        now=datetime(2026, 3, 27, 12, 2, tzinfo=UTC),
    )
    analyze_payload = cast(dict[str, object], analyze_result.output)
    runtime.checkpoint.save_step_completion(
        task_id,
        "analyze",
        artifacts=[f"{case_name}-analysis.json"],
        budget_delta={"cost_usd": analyze_result.cost_usd},
    )

    render_result = runtime.runner.execute(
        task_id,
        "mock_report_render",
        tool_input={
            "summary": cast(str, analyze_payload["summary"]),
            "details": cast(list[str], analyze_payload["key_points"]),
            "sources": [{"title": query, "url": search_payload[0]["url"]}],
            "metadata": {"case": case_name},
        },
        now=datetime(2026, 3, 27, 12, 3, tzinfo=UTC),
    )
    runtime.checkpoint.save_step_completion(
        task_id,
        "render",
        artifacts=[f"{case_name}-report.md"],
        budget_delta={"cost_usd": render_result.cost_usd},
    )

    runtime.lifecycle.transition(task_id, TaskState.completed)
    memory = _completed_memory(
        task_id,
        goal,
        result={"markdown": render_result.output},
        evidence_count=evidence_count,
        rating=rating,
        total_cost=round(search_result.cost_usd + fetch_result.cost_usd + analyze_result.cost_usd + render_result.cost_usd, 4),
        total_duration_minutes=max(1.0, round((time.perf_counter() - started_at) / 60.0, 2)),
    )
    _write_feedback_record(runtime.memory_file, memory)

    summary_path: Path | None = None
    cli_output = ""
    if use_cli_status:
        runner = CliRunner()
        status_result = runner.invoke(
            app,
            [
                "status",
                task_id,
                "--lifecycle-store",
                str(runtime.lifecycle_store),
                "--checkpoint-dir",
                str(runtime.checkpoint_dir),
                "--task-registry",
                str(runtime.task_registry),
                "--audit-log",
                str(runtime.audit_log),
                "--memory-file",
                str(runtime.memory_file),
                "--summary-dir",
                str(runtime.summary_dir),
            ],
        )
        assert status_result.exit_code == 0, status_result.output
        cli_output = status_result.output
        summary_path = runtime.summary_dir / task_id / "summary.md"
        assert summary_path.exists()
        assert "Summary:" in cli_output
        assert "Budget remaining:" in cli_output
    else:
        summary_report = runtime.observability.ensure_summary(task_id, goal=goal)
        summary_path = summary_report.summary_path
        assert summary_path.exists()

    return ScenarioResult(
        task_id=task_id,
        goal=goal,
        score=SelfEvaluationEngine(runtime.memory_file).evaluate(task_id).overall_score,
        duration_seconds=round(time.perf_counter() - started_at, 4),
        total_cost_usd=memory.metadata["total_cost"],
        summary_path=summary_path,
        cli_output=cli_output,
    )


def test_e2e_mock_scenarios_generate_completed_summaries(tmp_path: Path) -> None:
    scenarios = [
        {"case_name": "alpha", "query": "morphony release notes", "delay_seconds": 0.001, "evidence_count": 1, "rating": 4},
        {"case_name": "beta", "query": "budget tracking workflow", "delay_seconds": 0.002, "evidence_count": 2, "rating": 5},
        {"case_name": "gamma", "query": "checkpoint recovery plan", "delay_seconds": 0.003, "evidence_count": 3, "rating": 5},
    ]

    results = [
        _run_mock_research_flow(
            tmp_path,
            use_cli_status=True,
            **scenario,
        )
        for scenario in scenarios
    ]

    assert len(results) == 3
    assert all(result.summary_path is not None and result.summary_path.exists() for result in results)
    assert all(result.score >= 0.75 for result in results)
    assert all("Summary:" in result.cli_output for result in results)
    assert {result.task_id for result in results} == {
        "task-alpha",
        "task-beta",
        "task-gamma",
    }


def test_benchmark_suite_records_score_cost_and_duration_trends(tmp_path: Path) -> None:
    benchmark_cases = [
        BenchmarkCase(
            name=f"case-{index:02d}",
            query=f"benchmark query {index}",
            delay_seconds=0.005 * (index + 1),
            evidence_count=1 + (index // 3),
            rating=1 + min(4, index // 2),
        )
        for index in range(10)
    ]

    results: list[BenchmarkResult] = []
    for case in benchmark_cases:
        scenario_result = _run_mock_research_flow(
            tmp_path,
            case_name=case.name,
            query=case.query,
            delay_seconds=case.delay_seconds,
            evidence_count=case.evidence_count,
            rating=case.rating,
            use_cli_status=False,
        )
        results.append(
            BenchmarkResult(
                case=case,
                score=scenario_result.score,
                duration_seconds=scenario_result.duration_seconds,
                total_cost_usd=scenario_result.total_cost_usd,
            )
        )

    report = BenchmarkReport(results)

    assert len(report.results) == 10
    assert report.total_cost_usd == 0.0
    assert report.score_delta >= 0.0
    assert report.duration_delta > 0.0
    assert report.average_score >= report.results[0].score
    assert report.results[-1].score >= report.results[0].score
    assert report.results[-1].duration_seconds >= report.results[0].duration_seconds
