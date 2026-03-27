from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from morphony.config import load_config
from morphony.events import EventBus
from morphony.models import EpisodicMemory, SemanticMemory, TaskState

from .semantic_store import SemanticMemoryRecord, SemanticMemoryStore
from .store import EpisodicMemoryStore


_TOKEN_PATTERN = re.compile(r"[0-9A-Za-zぁ-んァ-ヶ一-龠_]+")


@dataclass(slots=True)
class MemoryPatternExtractor:
    episodic_store: EpisodicMemoryStore
    semantic_store: SemanticMemoryStore
    threshold: int = 3

    @classmethod
    def from_paths(
        cls,
        episodic_store_path: str | Path,
        semantic_store_path: str | Path,
        *,
        threshold: int | None = None,
        event_bus: EventBus | None = None,
    ) -> "MemoryPatternExtractor":
        config = load_config()
        resolved_threshold = threshold if threshold is not None else config.memory.hot_episodes
        return cls(
            episodic_store=EpisodicMemoryStore(episodic_store_path),
            semantic_store=SemanticMemoryStore(semantic_store_path, event_bus=event_bus),
            threshold=resolved_threshold,
        )

    def sync_all(self) -> list[SemanticMemoryRecord]:
        episodes_by_category = self._episodes_by_category()
        created: list[SemanticMemoryRecord] = []
        for category in sorted(episodes_by_category):
            record = self.sync_category(category)
            if record is not None:
                created.append(record)
        return created

    def sync_category(self, category: str) -> SemanticMemoryRecord | None:
        episodes = self._episodes_for_category(category)
        if len(episodes) < self.threshold:
            return None
        if self.semantic_store.search(category=category):
            return None
        memory = self._build_semantic_memory(category, episodes)
        return self.semantic_store.create(memory)

    def _episodes_by_category(self) -> dict[str, list[EpisodicMemory]]:
        grouped: dict[str, list[EpisodicMemory]] = defaultdict(list)
        for episode in self.episodic_store.list():
            category = _episode_category(episode)
            if category is None:
                continue
            grouped[category].append(episode)
        return dict(grouped)

    def _episodes_for_category(self, category: str) -> list[EpisodicMemory]:
        return [
            episode
            for episode in self.episodic_store.list()
            if _episode_category(episode) == category
        ]

    def _build_semantic_memory(
        self,
        category: str,
        episodes: list[EpisodicMemory],
    ) -> SemanticMemory:
        source_episode_ids = [episode.task_id for episode in episodes]
        common_tokens = _common_goal_tokens(episodes)
        common_phrase = " ".join(common_tokens[:4]) if common_tokens else category
        pattern_id = _pattern_id_for_category(category, source_episode_ids)
        pattern = f"Recurring lesson for {category}: {common_phrase}"
        conditions = common_tokens[:5] if common_tokens else [category]
        actions = [f"Apply the recurring {category} pattern"]
        confidence = round(min(1.0, 0.4 + 0.1 * len(common_tokens)), 2)
        success_rate = round(_average_success_rate(episodes), 2)

        return SemanticMemory(
            version=1,
            pattern_id=pattern_id,
            category=category,
            pattern=pattern,
            conditions=conditions,
            actions=actions,
            success_rate=success_rate,
            metadata={
                "confidence": confidence,
                "source_episodes": source_episode_ids,
                "episode_count": len(episodes),
            },
        )


def _episode_category(episode: EpisodicMemory) -> str | None:
    category = episode.metadata.get("category")
    if isinstance(category, str) and category.strip():
        return category
    return None


def _tokenize(text: str) -> set[str]:
    return {token.casefold() for token in _TOKEN_PATTERN.findall(text) if token}


def _common_goal_tokens(episodes: list[EpisodicMemory]) -> list[str]:
    if not episodes:
        return []
    token_sets = [_tokenize(episode.goal) for episode in episodes]
    if not token_sets:
        return []
    common = set(token_sets[0])
    for token_set in token_sets[1:]:
        common &= token_set
    if common:
        return sorted(common)

    frequency: dict[str, int] = {}
    for token_set in token_sets:
        for token in token_set:
            frequency[token] = frequency.get(token, 0) + 1
    ranked = [
        token
        for token, count in sorted(
            frequency.items(),
            key=lambda item: (-item[1], item[0]),
        )
        if count >= 2
    ]
    return ranked


def _average_success_rate(episodes: list[EpisodicMemory]) -> float:
    if not episodes:
        return 0.0
    total = 0.0
    for episode in episodes:
        total += _state_score(episode.execution_state)
    return total / len(episodes)


def _state_score(state: TaskState) -> float:
    if state == TaskState.completed:
        return 1.0
    if state in {TaskState.failed, TaskState.stopped}:
        return 0.0
    if state in {TaskState.running, TaskState.paused, TaskState.suspended}:
        return 0.5
    return 0.75


def _pattern_id_for_category(category: str, source_episode_ids: list[str]) -> str:
    digest_input = "|".join([category, *sorted(source_episode_ids)])
    digest = hashlib.sha1(digest_input.encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^0-9A-Za-z]+", "-", category.casefold()).strip("-")
    return f"semantic-{slug}-{digest}"
