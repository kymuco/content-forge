from __future__ import annotations

import pytest

from content_forge.application import (
    LongFormSharedSceneConflictError,
    LongFormSharedSceneWorkflow,
    ProjectVoicedSceneManifest,
    ProjectVoicedScenePlan,
    VoicedSceneScenePlan,
    capture_shared_voiced_scene,
    resolve_shared_voiced_scene,
)
from content_forge.application.voiced_scene_hardening import VoicedSceneWorkflow
from content_forge.core import (
    Asset,
    AssetRef,
    MediaType,
    Project,
    ProjectState,
    Scene,
    SourceRecord,
)
from content_forge.storage import LocalLibrary


def _source() -> tuple[Project, SourceRecord, Asset]:
    asset = Asset(
        sha256="9" * 64,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
        size_bytes=1,
        width=32,
        height=18,
    )
    record = SourceRecord(
        asset_id=asset.asset_id,
        platform="fixture",
        creator_name="original creator",
        permission_note="initial permission evidence",
    )
    ref = AssetRef(asset_id=asset.asset_id, source_id=record.source_id)
    return (
        Project(
            content_kind="voiced_source",
            source_refs=(ref,),
            source_records=(record,),
            scenes=(Scene(order=0, duration_seconds=1.0, media=ref),),
        ),
        record,
        asset,
    )


def _manifest(project: Project) -> ProjectVoicedSceneManifest:
    scene = project.scenes[0]
    return ProjectVoicedSceneManifest(
        project_id=project.project_id,
        plan=ProjectVoicedScenePlan(
            project_id=project.project_id,
            pr22_manifest_sha256="a" * 64,
            scenes=(
                VoicedSceneScenePlan(
                    scene_id=scene.scene_id,
                    pr22_scene_sha256="b" * 64,
                    camera_action="retain",
                    camera_source="none",
                ),
            ),
        ),
    )


def test_pr24_shared_reference_and_materialization_fail_if_source_provenance_changes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    source, record, asset = _source()
    host = Project(
        content_kind="long_form_fixture",
        state=ProjectState.DRAFT,
        scenes=(Scene(order=0, duration_seconds=1.0),),
    )
    library.database.put_asset(asset)
    library.save_project(source)
    library.save_project(host)
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
    changed_record = record.validated_copy(
        update={"permission_note": "updated permission evidence"}
    )
    library.save_project(
        source.validated_copy(update={"source_records": (changed_record,)})
    )

    with pytest.raises(LongFormSharedSceneConflictError, match="provenance changed"):
        resolve_shared_voiced_scene(library, reference)
    with pytest.raises(LongFormSharedSceneConflictError, match="provenance changed"):
        LongFormSharedSceneWorkflow(library).materialize(host.project_id, (reference,))


def test_pr24_shared_materialize_requires_explicit_nonempty_reference_set(tmp_path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    host = Project(
        content_kind="long_form_fixture",
        state=ProjectState.DRAFT,
        scenes=(Scene(order=0, duration_seconds=1.0),),
    )
    library.save_project(host)

    with pytest.raises(LongFormSharedSceneConflictError, match="at least one reference"):
        LongFormSharedSceneWorkflow(library).materialize(host.project_id, ())
