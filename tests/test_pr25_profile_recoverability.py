from __future__ import annotations

from pathlib import Path

import pytest

from content_forge.application import (
    ProductionProfileConflictError,
    ProductionProfileDefinition,
    ProductionProfileRegistry,
    ProductionProfileWorkflow,
    ProfileAssetPin,
)
from content_forge.core import MediaType, Project, ProjectState
from content_forge.storage import LocalLibrary
from content_forge.templates import create_builtin_registries


def test_pr25_unbind_remains_possible_after_old_optional_asset_disappears(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    registries = create_builtin_registries()
    music_path = tmp_path / "music.wav"
    music_path.write_bytes(b"pr25 optional music fixture")
    ingested = library.assets.ingest_file(
        music_path,
        media_type=MediaType.AUDIO,
        mime_type="audio/wav",
    )
    revision = ProductionProfileRegistry(library, registries).put(
        ProductionProfileDefinition(
            profile_id="recoverable_channel",
            scope="channel",
            display_name="Recoverable channel",
            music_library=(
                ProfileAssetPin(
                    asset_id=ingested.asset.asset_id,
                    sha256=ingested.asset.sha256,
                    role="background_music",
                ),
            ),
        )
    )
    project = library.save_project(
        Project(content_kind="profile_fixture", state=ProjectState.DRAFT)
    )
    workflow = ProductionProfileWorkflow(library, registries)
    workflow.bind(project.project_id, revision.profile_id, revision=revision.revision)

    library.assets.resolve(ingested.asset).unlink()

    with pytest.raises(ProductionProfileConflictError, match="bytes are unavailable"):
        workflow.manifest(project.project_id)

    restored = workflow.unbind(project.project_id)
    assert "pr25_production_profile" not in restored.metadata


def test_pr25_rebind_can_escape_old_missing_optional_asset(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    registries = create_builtin_registries()
    music_path = tmp_path / "music.wav"
    music_path.write_bytes(b"pr25 old music fixture")
    ingested = library.assets.ingest_file(
        music_path,
        media_type=MediaType.AUDIO,
        mime_type="audio/wav",
    )
    registry = ProductionProfileRegistry(library, registries)
    old = registry.put(
        ProductionProfileDefinition(
            profile_id="old_channel",
            scope="channel",
            display_name="Old channel",
            music_library=(
                ProfileAssetPin(
                    asset_id=ingested.asset.asset_id,
                    sha256=ingested.asset.sha256,
                    role="background_music",
                ),
            ),
        )
    )
    replacement = registry.put(
        ProductionProfileDefinition(
            profile_id="new_channel",
            scope="channel",
            display_name="New channel",
        )
    )
    project = library.save_project(
        Project(content_kind="profile_fixture", state=ProjectState.DRAFT)
    )
    workflow = ProductionProfileWorkflow(library, registries)
    workflow.bind(project.project_id, old.profile_id, revision=old.revision)
    library.assets.resolve(ingested.asset).unlink()

    rebound = workflow.bind(
        project.project_id,
        replacement.profile_id,
        revision=replacement.revision,
    )
    assert rebound.revision == replacement
