from __future__ import annotations

import re
from decimal import Decimal
from json import dumps
from typing import Callable, cast
from urllib.parse import quote_plus

import httpx

from morphony.models import EscalationLevel


SearchResult = dict[str, str]
SearchProvider = Callable[[str, int], list[SearchResult]]
Fetcher = Callable[[str, float], str]


def _default_search_provider(query: str, limit: int) -> list[SearchResult]:
    normalized_limit = max(1, limit)
    encoded_query = quote_plus(query.strip())
    results: list[SearchResult] = []
    for index in range(normalized_limit):
        rank = index + 1
        results.append(
            {
                "title": f"{query} - Result {rank}",
                "url": f"https://example.com/search?q={encoded_query}&rank={rank}",
                "snippet": f"Synthetic result {rank} for query '{query}'.",
            }
        )
    return results


def _default_fetcher(url: str, timeout_seconds: float) -> str:
    response = httpx.get(url, timeout=timeout_seconds, follow_redirects=True)
    response.raise_for_status()
    return response.text


def _strip_html_tags(raw_text: str) -> str:
    no_script = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw_text)
    no_tags = re.sub(r"(?s)<[^>]*>", " ", no_script)
    collapsed = re.sub(r"\s+", " ", no_tags).strip()
    return collapsed


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?。！？])\s+", text.strip())
    sentences: list[str] = []
    for part in parts:
        cleaned = part.strip()
        if cleaned:
            sentences.append(cleaned)
    if sentences:
        return sentences
    if text.strip():
        return [text.strip()]
    return []


def _normalize_object_list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be a list")
    return cast(list[object], value)


def _normalize_object_dict(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a mapping")
    return cast(dict[str, object], value)


class WebSearchTool:
    name = "web_search"
    description = "Search the web and return title/url/snippet results."
    risk_level = EscalationLevel.L1
    cost_per_call = Decimal("0.01")
    is_reversible = True

    def __init__(self, search_provider: SearchProvider | None = None) -> None:
        self._search_provider = search_provider if search_provider is not None else _default_search_provider

    def validate(self, *args: object, **kwargs: object) -> bool:
        query = kwargs.get("query")
        if not isinstance(query, str):
            return False
        return bool(query.strip())

    def execute(self, *args: object, **kwargs: object) -> list[SearchResult]:
        query = kwargs.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        raw_limit = kwargs.get("limit", 5)
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
            raise TypeError("limit must be an integer")
        if raw_limit < 1:
            raise ValueError("limit must be >= 1")
        return self._search_provider(query.strip(), raw_limit)

    def health_check(self) -> bool:
        try:
            self._search_provider("health check", 1)
        except Exception:
            return False
        return True


class WebFetchTool:
    name = "web_fetch"
    description = "Fetch a web page and return normalized text."
    risk_level = EscalationLevel.L1
    cost_per_call = Decimal("0.01")
    is_reversible = True

    def __init__(self, fetcher: Fetcher | None = None, default_timeout_seconds: float = 30.0) -> None:
        if default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be > 0")
        self._fetcher = fetcher if fetcher is not None else _default_fetcher
        self._default_timeout_seconds = default_timeout_seconds

    def validate(self, *args: object, **kwargs: object) -> bool:
        url = kwargs.get("url")
        if not isinstance(url, str):
            return False
        normalized = url.strip().lower()
        return normalized.startswith("http://") or normalized.startswith("https://")

    def execute(self, *args: object, **kwargs: object) -> dict[str, str]:
        url = kwargs.get("url")
        if not isinstance(url, str):
            raise TypeError("url must be a string")
        normalized_url = url.strip()
        if not normalized_url:
            raise ValueError("url must not be empty")
        if not (normalized_url.lower().startswith("http://") or normalized_url.lower().startswith("https://")):
            raise ValueError("url must start with http:// or https://")

        raw_timeout = kwargs.get("timeout_seconds", self._default_timeout_seconds)
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
            raise TypeError("timeout_seconds must be a number")
        timeout_seconds = float(raw_timeout)
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")

        raw_html = self._fetcher(normalized_url, timeout_seconds)
        return {
            "url": normalized_url,
            "text": _strip_html_tags(raw_html),
        }

    def health_check(self) -> bool:
        return True


class LlmAnalyzeTool:
    name = "llm_analyze"
    description = "Analyze text and return summary/key points with estimated token cost."
    risk_level = EscalationLevel.L1
    cost_per_call = Decimal("0.02")
    is_reversible = True

    def __init__(self, token_cost_per_1k_usd: float = 0.002) -> None:
        if token_cost_per_1k_usd < 0:
            raise ValueError("token_cost_per_1k_usd must be >= 0")
        self._token_cost_per_1k_usd = token_cost_per_1k_usd

    def validate(self, *args: object, **kwargs: object) -> bool:
        text = kwargs.get("text")
        instruction = kwargs.get("instruction", "summarize")
        return (
            isinstance(text, str)
            and bool(text.strip())
            and isinstance(instruction, str)
            and bool(instruction.strip())
        )

    def execute(self, *args: object, **kwargs: object) -> dict[str, object]:
        text = kwargs.get("text")
        instruction = kwargs.get("instruction", "summarize")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("instruction must be a non-empty string")
        raw_max_points = kwargs.get("max_points", 5)
        if isinstance(raw_max_points, bool) or not isinstance(raw_max_points, int):
            raise TypeError("max_points must be an integer")
        max_points = max(1, raw_max_points)

        sentences = _split_sentences(text)
        summary = sentences[0] if sentences else text.strip()[:240]
        key_points = sentences[:max_points]
        estimated_tokens = max(1, (len(text) + len(instruction)) // 4)
        estimated_cost_usd = round(
            (estimated_tokens / 1000.0) * self._token_cost_per_1k_usd,
            6,
        )
        self.cost_per_call = Decimal(str(estimated_cost_usd))
        return {
            "instruction": instruction,
            "summary": summary,
            "key_points": key_points,
            "estimated_tokens": estimated_tokens,
            "estimated_cost_usd": estimated_cost_usd,
        }

    def health_check(self) -> bool:
        return True


class ReportRenderTool:
    name = "report_render"
    description = "Render structured report data into Markdown."
    risk_level = EscalationLevel.L1
    cost_per_call = Decimal("0.005")
    is_reversible = True

    def validate(self, *args: object, **kwargs: object) -> bool:
        summary = kwargs.get("summary")
        details = kwargs.get("details")
        sources = kwargs.get("sources")
        metadata = kwargs.get("metadata")
        has_summary = isinstance(summary, str) and bool(summary.strip())
        has_details = isinstance(details, list) and len(cast(list[object], details)) > 0
        has_sources = isinstance(sources, list) and len(cast(list[object], sources)) > 0
        has_metadata = isinstance(metadata, dict) and len(cast(dict[str, object], metadata)) > 0
        return has_summary or has_details or has_sources or has_metadata

    def execute(self, *args: object, **kwargs: object) -> str:
        summary_raw = kwargs.get("summary", "")
        details_raw = _normalize_object_list(kwargs.get("details", []), "details")
        sources_raw = _normalize_object_list(kwargs.get("sources", []), "sources")
        metadata_raw = _normalize_object_dict(kwargs.get("metadata", {}), "metadata")

        if not isinstance(summary_raw, str):
            raise TypeError("summary must be a string")

        detail_lines: list[str] = []
        for detail in details_raw:
            if isinstance(detail, str) and detail.strip():
                detail_lines.append(f"- {detail.strip()}")

        source_lines: list[str] = []
        for source in sources_raw:
            if isinstance(source, str) and source.strip():
                source_lines.append(f"- {source.strip()}")
                continue
            if isinstance(source, dict):
                source_mapping = cast(dict[str, object], source)
                title = source_mapping.get("title")
                url = source_mapping.get("url")
                if isinstance(title, str) and isinstance(url, str) and title.strip() and url.strip():
                    source_lines.append(f"- [{title.strip()}]({url.strip()})")

        if not detail_lines:
            detail_lines = ["- (none)"]
        if not source_lines:
            source_lines = ["- (none)"]
        summary = summary_raw.strip() if summary_raw.strip() else "(none)"
        metadata_json = dumps(metadata_raw, ensure_ascii=False, indent=2, sort_keys=True)

        return "\n".join(
            [
                "## Summary",
                summary,
                "",
                "## Details",
                *detail_lines,
                "",
                "## Sources",
                *source_lines,
                "",
                "## Metadata",
                "```json",
                metadata_json,
                "```",
            ]
        )

    def health_check(self) -> bool:
        return True


__all__ = [
    "LlmAnalyzeTool",
    "ReportRenderTool",
    "WebFetchTool",
    "WebSearchTool",
]
