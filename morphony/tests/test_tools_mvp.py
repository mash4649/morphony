from __future__ import annotations

from typing import Any
from datetime import UTC, datetime
from typing import cast

from morphony.events import Event, EventBus, EventType
from morphony.models import Tool
from morphony.safety import BudgetController, EscalationEngine
from morphony.tools import (
    LlmAnalyzeTool,
    ReportRenderTool,
    ToolExecutionRunner,
    ToolRegistry,
    WebFetchTool,
    WebSearchTool,
)
def _assert_tool_interface(tool: Tool) -> None:
    assert isinstance(tool, Tool)
    assert isinstance(tool.name, str) and tool.name
    assert isinstance(tool.description, str) and tool.description
    assert hasattr(tool, "validate")
    assert hasattr(tool, "execute")
    assert hasattr(tool, "health_check")
    assert isinstance(tool.health_check(), bool)


def test_mvp_tools_conform_to_tool_interface_and_health_checks() -> None:
    def search_provider(query: str, limit: int) -> list[dict[str, str]]:
        return [
            {
                "title": f"{query} result {index}",
                "url": f"https://example.com/{query}/{index}",
                "snippet": f"Snippet {index} for {query}",
            }
            for index in range(1, limit + 1)
        ]

    def fetcher(url: str, timeout_seconds: float) -> str:
        _ = timeout_seconds
        return "<html><body><h1>Hello</h1><p>World</p></body></html>"

    tools: list[Tool] = [
        WebSearchTool(search_provider=search_provider),
        WebFetchTool(fetcher=fetcher),
        LlmAnalyzeTool(),
        ReportRenderTool(),
    ]

    for tool in tools:
        _assert_tool_interface(tool)

    search_tool = cast(WebSearchTool, tools[0])
    assert search_tool.validate(query="morphony") is True
    search_results = search_tool.execute(query="morphony", limit=2)
    assert search_tool.health_check() is True
    assert len(search_results) == 2
    for item in search_results:
        assert set(item) >= {"title", "url", "snippet"}
        assert all(isinstance(item[key], str) and item[key] for key in ("title", "url", "snippet"))

    fetch_tool = cast(WebFetchTool, tools[1])
    assert fetch_tool.validate(url="https://example.com/page") is True
    fetch_result = fetch_tool.execute(url="https://example.com/page")
    assert fetch_tool.health_check() is True
    assert fetch_result["url"] == "https://example.com/page"
    assert "<" not in fetch_result["text"]
    assert "Hello" in fetch_result["text"]
    assert "World" in fetch_result["text"]

    analyze_tool = cast(LlmAnalyzeTool, tools[2])
    assert analyze_tool.validate(text="Short text to summarize.", instruction="summarize") is True
    analyze_result = analyze_tool.execute(text="Short text to summarize.", instruction="summarize")
    assert analyze_tool.health_check() is True
    assert isinstance(analyze_result["estimated_cost_usd"], float)
    assert analyze_result["estimated_cost_usd"] > 0

    report_tool = cast(ReportRenderTool, tools[3])
    assert (
        report_tool.validate(
            summary="Summary",
            details=["Detail 1", "Detail 2"],
            sources=[{"title": "Source", "url": "https://example.com"}],
            metadata={"kind": "mvp"},
        )
        is True
    )
    report_markdown = report_tool.execute(
        summary="Summary",
        details=["Detail 1", "Detail 2"],
        sources=[{"title": "Source", "url": "https://example.com"}],
        metadata={"kind": "mvp"},
    )
    assert report_tool.health_check() is True
    assert "## Summary" in report_markdown
    assert "## Details" in report_markdown
    assert "## Sources" in report_markdown
    assert "## Metadata" in report_markdown

    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool, require_approval=False)
    assert registry.health_check_all() == {
        "llm_analyze": True,
        "report_render": True,
        "web_fetch": True,
        "web_search": True,
    }


def test_web_search_returns_title_url_snippet_results() -> None:
    def search_provider(query: str, limit: int) -> list[dict[str, str]]:
        return [
            {
                "title": f"{query} title {rank}",
                "url": f"https://search.example/{rank}",
                "snippet": f"{query} snippet {rank}",
            }
            for rank in range(1, limit + 1)
        ]

    tool = WebSearchTool(search_provider=search_provider)
    results = tool.execute(query="morphony", limit=3)

    assert len(results) == 3
    for result in results:
        assert set(result) >= {"title", "url", "snippet"}
        assert result["title"]
        assert result["url"].startswith("https://")
        assert result["snippet"]


def test_web_fetch_strips_html_without_network() -> None:
    seen_calls: list[tuple[str, float]] = []

    def fetcher(url: str, timeout_seconds: float) -> str:
        seen_calls.append((url, timeout_seconds))
        return "<html><body><main><h1>Alpha</h1><p>Beta <strong>Gamma</strong></p></main></body></html>"

    tool = WebFetchTool(fetcher=fetcher)
    result = tool.execute(url="https://example.com/docs", timeout_seconds=1.5)

    assert seen_calls == [("https://example.com/docs", 1.5)]
    assert result["url"] == "https://example.com/docs"
    assert "<" not in result["text"]
    assert "Alpha" in result["text"]
    assert "Beta" in result["text"]
    assert "Gamma" in result["text"]


def test_llm_analyze_cost_flows_through_runner_and_budget_controller() -> None:
    bus = EventBus()
    captured_events: list[Event] = []
    bus.subscribe_all(captured_events.append)
    escalation_engine = EscalationEngine(event_bus=bus)
    budget_controller = BudgetController(event_bus=bus)
    registry = ToolRegistry(escalation_engine=escalation_engine)
    tool = LlmAnalyzeTool()
    registry.register(tool, require_approval=False)

    runner = ToolExecutionRunner(
        registry,
        event_bus=bus,
        budget_controller=budget_controller,
        escalation_engine=escalation_engine,
    )
    result = runner.execute(
        "task-llm-analyze",
        "llm_analyze",
        tool_input={
            "text": "Short text to summarize.",
            "instruction": "summarize",
        },
        now=datetime(2026, 3, 26, 10, 0, tzinfo=UTC),
    )

    assert result.status == "succeeded"
    assert isinstance(result.output, dict)

    estimated_cost: float = float(cast(Any, result.output)["estimated_cost_usd"])  # pyright: ignore[reportUnknownMemberType]
    assert estimated_cost > 0
    assert result.cost_usd == estimated_cost

    snapshot = budget_controller.snapshot(
        "task-llm-analyze",
        now=datetime(2026, 3, 26, 10, 0, tzinfo=UTC),
    )
    assert snapshot.task.cost_usd == estimated_cost
    assert snapshot.daily.cost_usd == estimated_cost
    assert snapshot.monthly.cost_usd == estimated_cost

    budget_events = [event for event in captured_events if event.event_type == EventType.budget_consumed]
    assert len(budget_events) == 1
    budget_payload = cast(Any, budget_events[0].payload)
    consumed_cost: float = float(budget_payload["consumed"]["cost_usd"])  # pyright: ignore[reportUnknownMemberType]
    assert consumed_cost == estimated_cost
    assert budget_payload["tool_name"] == "llm_analyze"


def test_report_render_emits_expected_markdown_sections() -> None:
    tool = ReportRenderTool()
    markdown = tool.execute(
        summary="Research summary",
        details=["First detail", "Second detail"],
        sources=[
            {"title": "OpenAI", "url": "https://openai.com"},
            {"title": "Morphony", "url": "https://example.com/morphony"},
        ],
        metadata={"generated_by": "tests/test_tools_mvp.py"},
    )

    assert markdown.startswith("## Summary")
    assert "## Summary" in markdown
    assert "## Details" in markdown
    assert "## Sources" in markdown
    assert "## Metadata" in markdown
