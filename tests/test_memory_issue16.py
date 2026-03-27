from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from morphony.cli import app
from morphony.memory import EpisodicMemoryStore, MemoryPatternExtractor, SemanticMemoryStore
from morphony.models import EpisodicMemory, TaskState


def _episode(task_id: str, goal: str, category: str, *, state: TaskState = TaskState.completed) -> EpisodicMemory:
    return EpisodicMemory(
        task_id=task_id,
        goal=goal,
        execution_state=state,
        metadata={"category": category},
    )


def test_extractor_triggers_after_three_same_category_episodes(tmp_path: Path) -> None:
    episodic_store_path = tmp_path / "episodic.json"
    semantic_store_path = tmp_path / "semantic.json"
    episodic_store = EpisodicMemoryStore(episodic_store_path)

    episodic_store.create(_episode("ep-1", "collect research notes", "research"))
    episodic_store.create(_episode("ep-2", "collect research references", "research"))
    episodic_store.create(_episode("ep-3", "collect research sources", "research"))

    extractor = MemoryPatternExtractor.from_paths(
        episodic_store_path,
        semantic_store_path,
        threshold=3,
    )

    created = extractor.sync_category("research")
    assert created is not None
    assert created.memory.category == "research"
    assert created.memory.metadata["source_episodes"] == ["ep-1", "ep-2", "ep-3"]

    semantic_store = SemanticMemoryStore(semantic_store_path)
    results = semantic_store.search(category="research")
    assert len(results) == 1
    assert results[0].pattern_id == created.memory.pattern_id


def test_memory_cli_list_and_show_include_source_episode_details(tmp_path: Path) -> None:
    episodic_store_path = tmp_path / "episodic.json"
    semantic_store_path = tmp_path / "semantic.json"
    episodic_store = EpisodicMemoryStore(episodic_store_path)

    episodic_store.create(_episode("research-1", "collect research notes", "research"))
    episodic_store.create(_episode("research-2", "collect research references", "research"))
    episodic_store.create(_episode("research-3", "collect research sources", "research"))
    episodic_store.create(_episode("ops-1", "review deployment notes", "ops"))
    episodic_store.create(_episode("ops-2", "review deployment checklist", "ops"))
    episodic_store.create(_episode("ops-3", "review deployment rollback", "ops"))

    extractor = MemoryPatternExtractor.from_paths(
        episodic_store_path,
        semantic_store_path,
        threshold=3,
    )
    created = extractor.sync_all()
    assert len(created) == 2
    created_by_category = {record.memory.category: record.memory.pattern_id for record in created}

    runner = CliRunner()
    list_result = runner.invoke(
        app,
        [
            "memory",
            "list",
            "--semantic-store",
            str(semantic_store_path),
        ],
    )
    assert list_result.exit_code == 0, list_result.output
    assert "Semantic Memories" in list_result.output
    assert "research" in list_result.output
    assert "ops" in list_result.output
    assert list_result.output.count("3") >= 2

    show_result = runner.invoke(
        app,
        [
            "memory",
            "show",
            created_by_category["ops"],
            "--semantic-store",
            str(semantic_store_path),
            "--episodic-store",
            str(episodic_store_path),
        ],
    )
    assert show_result.exit_code == 0, show_result.output
    assert created_by_category["ops"] in show_result.output
    assert "Pattern" in show_result.output
    assert "Source Episodes" in show_result.output
    assert "Linked Episodic Memories" in show_result.output
    assert "ops-1" in show_result.output
    assert "review deployment notes" in show_result.output
