from __future__ import annotations

import pytest

from content_forge.application import (
    LongFormSharedSceneConflictError,
    LongFormSharedSceneWorkflow,
    ProjectVoicedSceneManifest,
    ProjectVoicedScenePlan,
    VoicedSceneScenePlan,
    capture_shared_voiced_scene,
    long_form_shared_manifest,
)
from content_forge.application.voiced_scene_hardening import VoicedSceneWorkflow
from content_forge.core import (
    Asset,
    AssetRef,
    AudioTrack,
    MediaType,
    MotionSpec,
    Overlay,
    Project,
    ProjectState,
    Scene,
    SourceRecord,
)
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


def _source_with_provenance(
    *,
    sha_char: str,
) -> tuple[Project, SourceRecord]:
    asset = Asset(
        sha256=sha_char * 64,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
        size_bytes=1,
        width=100,
        height=100,
    )
    record = SourceRecord(asset_id=asset.asset_id, platform="fixture")
    ref = AssetRef(asset_id=asset.asset_id, source_id=record.source_id)
    scene = Scene(
        order=0,
        duration_seconds=2.0,
        media=ref,
        motion=MotionSpec(
            motion_type="focus_zoom",
            properties={"pr23_owner": "pr23_camera_v1", "start_scale": 1.0},
        ),
        overlays=(
            Overlay(
                component_type="timed_text",
                start_seconds=0.0,
                duration_seconds=1.0,
                text="hello",
                properties={"pr22_owner": "pr22_timed_text_v1"},
            ),
        ),
        audio_tracks=(
            AudioTrack(
                track_type="voice",
                asset_ref=ref,
                duration_seconds=1.0,
                properties={"pr22_owner": "pr22_voice_audio_v1"},
            ),
            AudioTrack(
                track_type="music",
                asset_ref=ref,
                duration_seconds=2.0,
                properties={
                    "pr23_owner": "pr23_voiced_mix_v1",
                    "duck_db": -10.0,
                },
            ),
        ),
    )
    return (
        Project(
            content_kind="voiced_source",
            source_refs=(ref,),
            source_records=(record,),
            scenes=(scene,),
        ),
        record,
    )


def _install_authority(
    monkeypatch: pytest.MonkeyPatch,
    manifests: dict[str, ProjectVoicedSceneManifest],
) -> None:
    def validate(self, project: Project) -> ProjectVoicedSceneManifest:
        return manifests[project.project_id]

    monkeypatch.setattr(VoicedSceneWorkflow, "validate_snapshot", validate)


def _host() -> Project:
    return Project(
        content_kind="long_form_fixture",
        state=ProjectState.DRAFT,
        scenes=(Scene(order=0, duration_seconds=1.0),),
    )


def test_pr24_shared_workflow_materializes_host_owned_scene_and_is_idempotent(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    source, _ = _source_with_provenance(sha_char="1")
    host = _host()
    library.save_project(source)
    library.save_project(host)
    _install_authority(monkeypatch, {source.project_id: _manifest(source)})

    reference = capture_shared_voiced_scene(
        library,
        source.project_id,
        source.scenes[0].scene_id,
    )
    workflow = LongFormSharedSceneWorkflow(library)
    first = workflow.materialize(host.project_id, (reference,))
    materialized = library.load_project(host.project_id)
    assert materialized is not None
    assert len(materialized.scenes) == 2
    copied = materialized.scenes[1]
    assert copied.order == 1
    assert copied.properties["pr24_source_project_id"] == source.project_id
    assert copied.properties["pr24_source_scene_id"] == source.scenes[0].scene_id
    assert copied.properties["pr24_owner"] == "pr24_shared_voiced_scene_v1"
    assert "pr23_owner" not in copied.motion.properties
    assert "pr22_owner" not in copied.overlays[0].properties
    assert "pr22_owner" not in copied.audio_tracks[0].properties
    assert "pr23_owner" not in copied.audio_tracks[1].properties
    assert copied.audio_tracks[1].properties["duck_db"] == -10.0
    assert first.bindings[0].materialized_scene_id == copied.scene_id

    before_repeat = materialized
    second = workflow.materialize(host.project_id, (reference,))
    after_repeat = library.load_project(host.project_id)
    assert second == first
    assert after_repeat == before_repeat


def test_pr24_shared_workflow_dematerialize_restores_host_and_removes_owned_provenance(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    source, record = _source_with_provenance(sha_char="2")
    host = _host()
    library.save_project(source)
    library.save_project(host)
    _install_authority(monkeypatch, {source.project_id: _manifest(source)})
    reference = capture_shared_voiced_scene(
        library,
        source.project_id,
        source.scenes[0].scene_id,
    )
    workflow = LongFormSharedSceneWorkflow(library)
    workflow.materialize(host.project_id, (reference,))

    with_shared = library.load_project(host.project_id)
    assert with_shared is not None
    assert any(item.source_id == record.source_id for item in with_shared.source_records)
    assert any(item.role == "pr24_shared_source" for item in with_shared.source_refs)

    assert workflow.dematerialize(host.project_id) is True
    restored = library.load_project(host.project_id)
    assert restored is not None
    assert restored.scenes == host.scenes
    assert restored.source_refs == host.source_refs
    assert restored.source_records == host.source_records
    assert long_form_shared_manifest(restored) is None
    assert "pr24_shared_voiced_scene_provenance" not in restored.metadata


def test_pr24_dematerialize_preserves_imported_record_if_host_adopts_it(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    source, record = _source_with_provenance(sha_char="3")
    host = _host()
    library.save_project(source)
    library.save_project(host)
    _install_authority(monkeypatch, {source.project_id: _manifest(source)})
    reference = capture_shared_voiced_scene(
        library,
        source.project_id,
        source.scenes[0].scene_id,
    )
    workflow = LongFormSharedSceneWorkflow(library)
    workflow.materialize(host.project_id, (reference,))

    current = library.load_project(host.project_id)
    assert current is not None
    local_ref = AssetRef(
        asset_id=record.asset_id,
        source_id=record.source_id,
        role="host_reuse",
    )
    adopted = current.validated_copy(
        update={"source_refs": (*current.source_refs, local_ref)}
    )
    library.save_project(adopted)

    assert workflow.dematerialize(host.project_id) is True
    restored = library.load_project(host.project_id)
    assert restored is not None
    assert local_ref in restored.source_refs
    assert record in restored.source_records
    assert not any(item.role == "pr24_shared_source" for item in restored.source_refs)


def test_pr24_shared_workflow_detects_owned_host_scene_drift(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    source, _ = _source_with_provenance(sha_char="4")
    host = _host()
    library.save_project(source)
    library.save_project(host)
    _install_authority(monkeypatch, {source.project_id: _manifest(source)})
    reference = capture_shared_voiced_scene(
        library,
        source.project_id,
        source.scenes[0].scene_id,
    )
    workflow = LongFormSharedSceneWorkflow(library)
    workflow.materialize(host.project_id, (reference,))

    current = library.load_project(host.project_id)
    assert current is not None
    changed_shared = current.scenes[1].validated_copy(update={"duration_seconds": 3.0})
    library.save_project(
        current.validated_copy(update={"scenes": (current.scenes[0], changed_shared)})
    )

    drifted = library.load_project(host.project_id)
    assert drifted is not None
    with pytest.raises(LongFormSharedSceneConflictError, match="materialization drifted"):
        workflow.validate_snapshot(drifted)


def test_pr24_replacing_shared_scene_removes_stale_pr24_provenance(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    first_source, first_record = _source_with_provenance(sha_char="5")
    second_source, second_record = _source_with_provenance(sha_char="6")
    host = _host()
    library.save_project(first_source)
    library.save_project(second_source)
    library.save_project(host)
    manifests = {
        first_source.project_id: _manifest(first_source),
        second_source.project_id: _manifest(second_source),
    }
    _install_authority(monkeypatch, manifests)
    first_ref = capture_shared_voiced_scene(
        library,
        first_source.project_id,
        first_source.scenes[0].scene_id,
    )
    second_ref = capture_shared_voiced_scene(
        library,
        second_source.project_id,
        second_source.scenes[0].scene_id,
    )
    workflow = LongFormSharedSceneWorkflow(library)
    workflow.materialize(host.project_id, (first_ref,))
    workflow.materialize(host.project_id, (second_ref,))

    current = library.load_project(host.project_id)
    assert current is not None
    record_ids = {item.source_id for item in current.source_records}
    assert first_record.source_id not in record_ids
    assert second_record.source_id in record_ids
    assert len(tuple(item for item in current.source_refs if item.role == "pr24_shared_source")) == 1
    manifest = long_form_shared_manifest(current)
    assert manifest is not None
    assert manifest.bindings[0].reference == second_ref
