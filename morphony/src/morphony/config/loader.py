from __future__ import annotations

from pathlib import Path
from collections.abc import Mapping
from typing import Any, cast

import yaml
from pydantic import ValidationError

from .schema import AgentConfig

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "configs" / "agent_config.yaml"

_REQUIRED_CONFIG_STRUCTURE: dict[str, Any] = {
    "escalation": {
        "l2_timeout_minutes": {},
        "l2_timeout_policy": {},
        "l3_reminder_minutes": {},
        "l3_auto_suspend_hours": {},
    },
    "budget": {
        "task": {
            "cost_usd": {},
            "time_minutes": {},
            "api_calls": {},
        },
        "daily": {
            "cost_usd": {},
            "time_hours": {},
        },
        "monthly": {
            "cost_usd": {},
        },
    },
    "improvement": {
        "max_iterations": {},
        "trigger_threshold": {},
        "completion_threshold": {},
    },
    "memory": {
        "hot_episodes": {},
        "semantic_max_per_category": {},
        "inactive_threshold_days": {},
    },
    "safety": {
        "sandbox_enabled": {},
        "kill_switch_enabled": {},
    },
}


def _collect_missing_keys(
    data: Mapping[str, Any], structure: Mapping[str, Any], prefix: str = ""
) -> list[str]:
    missing: list[str] = []
    for key, child_structure in structure.items():
        current_path = f"{prefix}.{key}" if prefix else key
        if key not in data:
            missing.append(current_path)
            continue
        value = data[key]
        if isinstance(child_structure, Mapping) and isinstance(value, Mapping):
            missing.extend(
                _collect_missing_keys(
                    cast(Mapping[str, Any], value),
                    cast(Mapping[str, Any], child_structure),
                    current_path,
                )
            )
    return missing


def _format_validation_error(exc: ValidationError, source: Path) -> ValueError:
    lines = [f"Invalid agent config at {source}:"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        message = error["msg"]
        if location:
            lines.append(f"- {location}: {message}")
        else:
            lines.append(f"- {message}")
    return ValueError("\n".join(lines))


def load_config(path: str | Path | None = None) -> AgentConfig:
    config_path = Path(path).expanduser() if path is not None else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return AgentConfig()

    try:
        raw_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - exercised by validation tests
        raise ValueError(f"Failed to parse YAML config at {config_path}: {exc}") from exc

    if raw_data is None:
        raw_data = {}
    if not isinstance(raw_data, dict):
        raise ValueError(
            f"Invalid agent config at {config_path}: root document must be a mapping"
        )

    raw_mapping = cast(dict[str, Any], raw_data)

    missing_keys = _collect_missing_keys(raw_mapping, _REQUIRED_CONFIG_STRUCTURE)
    if missing_keys:
        missing_list = ", ".join(missing_keys)
        raise ValueError(
            f"Invalid agent config at {config_path}: missing required fields: {missing_list}"
        )

    try:
        return AgentConfig.model_validate(raw_data)
    except ValidationError as exc:
        raise _format_validation_error(exc, config_path) from exc
