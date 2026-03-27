from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, cast

import yaml
from pydantic import ValidationError

from .schema import AgentConfig

logger = logging.getLogger(__name__)


def parse_runtime_overrides(overrides: Sequence[str] | None) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    if overrides is None:
        return parsed

    for raw_override in overrides:
        if not raw_override:
            raise ValueError("runtime override entries must not be empty")
        key, separator, raw_value = raw_override.partition("=")
        if not separator:
            raise ValueError(
                f"Invalid runtime override '{raw_override}'. Expected 'dot.path=value'."
            )
        key = key.strip()
        if not key:
            raise ValueError(f"Invalid runtime override '{raw_override}': missing key")
        try:
            parsed[key] = yaml.safe_load(raw_value.strip())
        except yaml.YAMLError as exc:  # pragma: no cover - defensive
            raise ValueError(f"Invalid runtime override value for '{key}': {exc}") from exc
    return parsed


def _walk_path(data: dict[str, Any], path: Sequence[str]) -> Any | None:
    current_mapping: Mapping[str, Any] = data
    for index, key in enumerate(path):
        if key not in current_mapping:
            return None
        value: Any = current_mapping.get(key)
        if index == len(path) - 1:
            return value
        if not isinstance(value, Mapping):
            return None
        current_mapping = cast(Mapping[str, Any], value)
    return None


def _set_path(data: dict[str, Any], path: Sequence[str], value: Any) -> None:
    current: dict[str, Any] = data
    for key in path[:-1]:
        next_value: Any = current.get(key)
        if next_value is None:
            next_container: dict[str, Any] = {}
            current[key] = next_container
            current = next_container
            continue
        if not isinstance(next_value, dict):
            raise ValueError(
                f"Cannot apply override to '{'.'.join(path)}': '{key}' is not a mapping"
            )
        current = cast(dict[str, Any], next_value)
    current[path[-1]] = value


def _safety_relaxation_detected(current_value: Any, override_value: Any) -> bool:
    if isinstance(current_value, dict) and isinstance(override_value, dict):
        current_mapping = cast(dict[str, Any], current_value)
        override_mapping = cast(dict[str, Any], override_value)
        for key, child_value in override_mapping.items():
            if key in current_mapping and _safety_relaxation_detected(
                current_mapping[key], child_value
            ):
                return True
        return False
    if isinstance(current_value, bool) and isinstance(override_value, bool):
        return current_value and not override_value
    return False


def apply_runtime_overrides(
    config: AgentConfig, overrides: Mapping[str, Any] | Sequence[str] | None
) -> AgentConfig:
    if overrides is None:
        return config

    if isinstance(overrides, str):
        raise TypeError("runtime overrides must be a mapping or a sequence of 'path=value' strings")

    parsed_overrides: Mapping[str, Any]
    if isinstance(overrides, Mapping):
        parsed_overrides = overrides
    else:
        parsed_overrides = parse_runtime_overrides(overrides)

    config_data: dict[str, Any] = config.model_dump(mode="python")

    for raw_path, value in parsed_overrides.items():
        path = tuple(part for part in raw_path.split(".") if part)
        if not path:
            raise ValueError("runtime override paths must not be empty")

        current_value = _walk_path(config_data, path)
        if path[0] == "safety" and _safety_relaxation_detected(current_value, value):
            raise ValueError(f"Refusing to relax safety override '{raw_path}'")

        _set_path(config_data, path, value)
        logger.info("Applied runtime override %s=%r", raw_path, value)

    try:
        return AgentConfig.model_validate(config_data)
    except ValidationError as exc:
        messages = ["Invalid runtime overrides:"]
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"])
            messages.append(f"- {location}: {error['msg']}")
        raise ValueError("\n".join(messages)) from exc
