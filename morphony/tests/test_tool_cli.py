from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from typer.testing import CliRunner

from morphony.cli import app


@dataclass(frozen=True, slots=True)
class ToolRow:
    name: str
    risk_level: str
    status: str


def _tool_rows(output: str) -> list[ToolRow]:
    rows: list[ToolRow] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        name, risk_level, status = parts
        if status not in {"UP", "DOWN"}:
            continue
        rows.append(ToolRow(name=name, risk_level=risk_level, status=status))
    return rows


def _assert_valid_tool_rows(output: str) -> list[ToolRow]:
    rows = _tool_rows(output)
    assert rows, output
    for row in rows:
        assert row.name
        assert row.risk_level
        assert row.status in {"UP", "DOWN"}
    return rows


def _write_plugin_module(path: Path) -> Path:
    path.write_text(
        """
from __future__ import annotations

from decimal import Decimal

from morphony.models import EscalationLevel


class Issue13Tool:
    name = "issue13_demo"
    description = "Plugin tool for Issue #13 CLI coverage"
    risk_level = EscalationLevel.L1
    cost_per_call = Decimal("0.25")
    is_reversible = True

    def execute(self, *args: object, **kwargs: object) -> dict[str, object]:
        _ = args, kwargs
        return {"status": "ok"}

    def validate(self, *args: object, **kwargs: object) -> bool:
        _ = args, kwargs
        return True

    def health_check(self) -> bool:
        return True


def create_tool() -> Issue13Tool:
    return Issue13Tool()
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return path


def test_tool_list_shows_builtin_tool_metadata(tmp_path: Path) -> None:
    runner = CliRunner()
    plugins_file = tmp_path / "runtime" / "tools" / "plugins.json"

    result = runner.invoke(
        app,
        [
            "tool",
            "list",
            "--plugins-file",
            str(plugins_file),
        ],
    )

    assert result.exit_code == 0, result.output
    rows = _assert_valid_tool_rows(result.output)

    expected_names = {"llm_analyze", "report_render", "web_fetch", "web_search"}
    names = {row.name for row in rows}
    assert expected_names <= names
    builtin_rows = [row for row in rows if row.name in expected_names]
    assert len(builtin_rows) == 4
    assert all(row.status in {"UP", "DOWN"} for row in builtin_rows)


def test_tool_add_loads_plugin_and_registers_after_l3_approval(tmp_path: Path) -> None:
    runner = CliRunner()
    plugins_file = tmp_path / "runtime" / "tools" / "plugins.json"
    plugin_path = _write_plugin_module(tmp_path / "issue13_demo.py")

    add_result = runner.invoke(
        app,
        [
            "tool",
            "add",
            str(plugin_path),
            "--plugins-file",
            str(plugins_file),
        ],
    )

    assert add_result.exit_code == 0, add_result.output
    assert "L3 approval requested" in add_result.output
    assert "L3 approval granted" in add_result.output
    assert "Tool 'issue13_demo' registered." in add_result.output

    list_result = runner.invoke(
        app,
        [
            "tool",
            "list",
            "--plugins-file",
            str(plugins_file),
        ],
    )

    assert list_result.exit_code == 0, list_result.output
    rows = _assert_valid_tool_rows(list_result.output)

    expected_names = {"llm_analyze", "report_render", "web_fetch", "web_search"}
    names = {row.name for row in rows}
    assert expected_names <= names
    assert "issue13_demo" in names

    plugin_rows = [row for row in rows if row.name == "issue13_demo"]
    assert len(plugin_rows) == 1
    assert plugin_rows[0].risk_level == "L1"
    assert plugin_rows[0].status in {"UP", "DOWN"}
