from __future__ import annotations

import pytest

from content_forge.application import (
    ProjectVoicedSceneManifest,
    ProjectVoicedScenePlan,
    VoicedSceneScenePlan,
)
from content_forge.application.long_form_shared import (
    LongFormSharedSceneConflictError,
    LongFormSharedSceneNotFoundError,
    capture_shared_voiced_scene,
    resolve_shared_voiced_scene,
)
from content_forge.application.voiced_scene_hardening import VoicedSceneWorkflow
from content_forge.core import Project, Scene
from content_forge.storage import LocalLibrary


def _manifest(project: Project, *, pr22_scene_sha: str = "b" * 64) -> ProjectVoicedSceneManifest:
    scene = project.scenes[0]
    return ProjectVoicedSceneManifest(
        project_id=project.project_id,
        plan=ProjectVoicedScenePlan(
            project_id=project.project_id,
            pr22_manifest_sha256="a" * 64,
            scenes=(
                VoicedSceneScenePlan(
                    scene_id=scene.scene_id,
                    pr22_scene_sha256=pr22_scene_sha,
                    camera_action="retain",
                    camera_source="none",
                ),
            ),
        ),
    )


def test_pr24_capture_and_resolve_pin_exact_scene_and_pr22_pr23_authority(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    source = Project(
        content_kind="voiced_source",
        scenes=(Scene(order=0, duration_seconds=2.0),),
    )
    library.save_project(source)
    manifest = _manifest(source)

    monkeypatch.setattr(
        VoicedSceneWorkflow,
        "validate_snapshot",
        lambda self, project: manifest,
    )

    reference = capture_shared_voiced_scene(
        library,
        source.project_id,
        source.scenes[0].scene_id,
    )
    resolved = resolve_shared_voiced_scene(library, reference)

    assert resolved == source.scenes[0]
    assert reference.source_project_id == source.project_id
    assert reference.source_scene_id == source.scenes[0].scene_id
    assert reference.pr22_scene_sha256 == "b" * 64


def test_pr24_shared_reference_fails_closed_if_source_scene_changes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    source = Project(
        content_kind="voiced_source",
        scenes=(Scene(order=0, duration_seconds=2.0),),
    )
    library.save_project(source)
    manifest = _manifest(source)
    monkeypatch.setattr(
        VoicedSceneWorkflow,
        "validate_snapshot",
        lambda self, project: manifest,
    )
    reference = capture_shared_voiced_scene(
        library,
        source.project_id,
        source.scenes[0].scene_id,
    )

    changed = source.validated_copy(
        update={
            "scenes": (
                source.scenes[0].validated_copy(update={"duration_seconds": 3.0}),
            ),
        }
    )
    library.save_project(changed)

    with pytest.raises(LongFormSharedSceneConflictError, match="Scene changed"):
        resolve_shared_voiced_scene(library, reference)


def test_pr24_shared_reference_fails_closed_if_pr22_or_pr23_scene_authority_changes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    source = Project(
        content_kind="voiced_source",
        scenes=(Scene(order=0, duration_seconds=2.0),),
    )
    library.save_project(source)
    original = _manifest(source)
    current = {"manifest": original}
    monkeypatch.setattr(
        VoicedSceneWorkflow,
        "validate_snapshot",
        lambda self, project: current["manifest"],
    )
    reference = capture_shared_voiced_scene(
        library,
        source.project_id,
        source.scenes[0].scene_id,
    )

    current["manifest"] = _manifest(source, pr22_scene_sha="c" * 64)
    with pytest.raises(LongFormSharedSceneConflictError, match="PR22"):
        resolve_shared_voiced_scene(library, reference)

    altered_scene_plan = original.plan.scenes[0].validated_copy(
        update={"camera_source": "speaker_unresolved"}
    )
    current["manifest"] = original.validated_copy(
        update={
            "plan": original.plan.validated_copy(update={"scenes": (altered_scene_plan,)})
        }
    )
    with pytest.raises(LongFormSharedSceneConflictError, match="PR23"):
        resolve_shared_voiced_scene(library, reference)


def test_pr24_shared_reference_requires_existing_source_project(tmp_path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    missing = Project(content_kind="temporary").project_id
    scene_id = Scene(order=0, duration_seconds=1.0).scene_id

    with pytest.raises(LongFormSharedSceneNotFoundError, match="unknown"):
        capture_shared_voiced_scene(library, missing, scene_id)
