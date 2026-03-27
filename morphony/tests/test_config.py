from __future__ import annotations

import importlib
import inspect
import logging
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError


VALID_CONFIG_YAML = """
escalation:
  l2_timeout_minutes: 15
  l2_timeout_policy: escalate
  l3_reminder_minutes: 30
  l3_auto_suspend_hours: 24
budget:
  task:
    cost_usd: 5
    time_minutes: 30
    api_calls: 100
  daily:
    cost_usd: 20
    time_hours: 4
  monthly:
    cost_usd: 200
improvement:
  max_iterations: 3
  trigger_threshold: 0.8
  completion_threshold: 0.9
memory:
  hot_episodes: 100
  semantic_max_per_category: 50
  inactive_threshold_days: 90
safety:
  sandbox_enabled: true
  kill_switch_enabled: true
"""


def _config_module() -> Any:
    return importlib.import_module("morphony.config")


def _write_yaml(path: Path, content: str) -> Path:
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return path


def _get_agent_config_class(module: Any) -> type[Any]:
    agent_config = getattr(module, "AgentConfig", None)
    assert agent_config is not None, "morphony.config.AgentConfig is required"
    return agent_config


def _call_loader(loader: Any, path: Path | None) -> Any:
    signature = inspect.signature(loader)
    params = signature.parameters

    if not params:
        return loader()

    if len(params) == 1:
        parameter = next(iter(params.values()))
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            return loader(path)
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
            return loader(**{parameter.name: path})

    if "path" in params:
        return loader(path=path)
    if "config_path" in params:
        return loader(config_path=path)
    if "config_file" in params:
        return loader(config_file=path)
    if "yaml_path" in params:
        return loader(yaml_path=path)

    return loader(path)


def _load_config(module: Any, path: Path | None) -> Any:
    for name in (
        "load_config",
        "load_agent_config",
        "load_from_yaml",
        "load_yaml",
    ):
        loader = getattr(module, name, None)
        if callable(loader):
            return _call_loader(loader, path)

    config_loader = getattr(module, "ConfigLoader", None)
    if config_loader is not None:
        loader = config_loader() if inspect.isclass(config_loader) else config_loader
        load = getattr(loader, "load", None)
        if callable(load):
            return _call_loader(load, path)

    raise AssertionError("morphony.config needs a config loading entrypoint")


def _apply_runtime_overrides(module: Any, config: Any, overrides: dict[str, Any]) -> Any:
    for name in (
        "apply_runtime_overrides",
        "apply_overrides",
        "with_runtime_overrides",
        "override_config",
    ):
        fn = getattr(module, name, None)
        if callable(fn):
            try:
                return fn(config, overrides)
            except TypeError:
                return fn(config, **overrides)

    for name in (
        "apply_runtime_overrides",
        "apply_overrides",
        "with_overrides",
    ):
        method = getattr(config, name, None)
        if callable(method):
            try:
                return method(overrides)
            except TypeError:
                return method(**overrides)

    raise AssertionError("morphony.config needs a runtime override entrypoint")


def test_valid_yaml_returns_agent_config(tmp_path: Path) -> None:
    module = _config_module()
    agent_config_cls = _get_agent_config_class(module)
    config_path = _write_yaml(tmp_path / "agent_config.yaml", VALID_CONFIG_YAML)

    config = _load_config(module, config_path)

    assert isinstance(config, agent_config_cls)
    assert config.safety.sandbox_enabled is True
    assert config.safety.kill_switch_enabled is True


def test_invalid_yaml_raises_clear_error(tmp_path: Path) -> None:
    module = _config_module()
    config_path = _write_yaml(
        tmp_path / "agent_config.yaml",
        """
        escalation:
          l2_timeout_minutes: 15
          l2_timeout_policy: escalate
          l3_reminder_minutes: 30
          l3_auto_suspend_hours: 24
        budget:
          task:
            cost_usd: 5
            time_minutes: 30
            api_calls: 100
          daily:
            cost_usd: 20
            time_hours: 4
          monthly:
            cost_usd: 200
        improvement:
          max_iterations: 3
          trigger_threshold: 0.8
          completion_threshold: 0.9
        memory:
          hot_episodes: 100
          semantic_max_per_category: 50
          inactive_threshold_days: 90
        safety:
          sandbox_enabled: true
          kill_switch_enabled: true
          broken: [
        """,
    )

    with pytest.raises((yaml.YAMLError, ValueError, ValidationError)) as exc_info:
        _load_config(module, config_path)

    message = str(exc_info.value).lower()
    assert "yaml" in message or "parse" in message or "invalid" in message


def test_missing_required_field_raises_clear_error(tmp_path: Path) -> None:
    module = _config_module()
    config_path = _write_yaml(
        tmp_path / "agent_config.yaml",
        """
        escalation:
          l2_timeout_minutes: 15
          l2_timeout_policy: escalate
          l3_reminder_minutes: 30
          l3_auto_suspend_hours: 24
        budget:
          task:
            time_minutes: 30
            api_calls: 100
          daily:
            cost_usd: 20
            time_hours: 4
          monthly:
            cost_usd: 200
        improvement:
          max_iterations: 3
          trigger_threshold: 0.8
          completion_threshold: 0.9
        memory:
          hot_episodes: 100
          semantic_max_per_category: 50
          inactive_threshold_days: 90
        safety:
          sandbox_enabled: true
          kill_switch_enabled: true
        """,
    )

    with pytest.raises((ValueError, ValidationError)) as exc_info:
        _load_config(module, config_path)

    message = str(exc_info.value).lower()
    assert "cost_usd" in message or "required" in message or "missing" in message


def test_missing_config_file_falls_back_to_defaults(tmp_path: Path) -> None:
    module = _config_module()
    agent_config_cls = _get_agent_config_class(module)
    missing_path = tmp_path / "missing-agent-config.yaml"

    config = _load_config(module, missing_path)

    assert isinstance(config, agent_config_cls)
    assert config.safety.sandbox_enabled is True
    assert config.safety.kill_switch_enabled is True


def test_runtime_override_is_applied_and_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _config_module()
    config_path = _write_yaml(tmp_path / "agent_config.yaml", VALID_CONFIG_YAML)
    config = _load_config(module, config_path)

    with caplog.at_level(logging.DEBUG):
        overridden = _apply_runtime_overrides(
            module,
            config,
            {"budget.task.cost_usd": 9, "memory.hot_episodes": 77},
        )

    assert overridden is not None
    assert overridden.budget.task.cost_usd == 9
    assert overridden.memory.hot_episodes == 77
    assert any("override" in record.message.lower() for record in caplog.records)
    assert any("budget.task.cost_usd" in record.message for record in caplog.records)


def test_relaxing_safety_sandbox_enabled_to_false_is_rejected(tmp_path: Path) -> None:
    module = _config_module()
    config_path = _write_yaml(tmp_path / "agent_config.yaml", VALID_CONFIG_YAML)
    config = _load_config(module, config_path)

    with pytest.raises((ValueError, PermissionError, ValidationError)) as exc_info:
        _apply_runtime_overrides(
            module,
            config,
            {"safety.sandbox_enabled": False},
        )

    message = str(exc_info.value).lower()
    assert "sandbox_enabled" in message or "safety" in message
    assert "false" in message or "disable" in message or "relax" in message
