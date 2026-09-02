from __future__ import annotations

import uuid

import pytest

from content_forge.api import create_app
from content_forge.application.production_presets import (
    ProductionPresetConflictError,
    ProductionPresetService,
    ProductionPresetValidationError,
    preset_for_project,
)
from content_forge.core import Asset, AssetRef, MediaType, Project, ProjectState, SourceRecord


def _source_project(library, *, asset: Asset, source_record: SourceRecord | None = None) -> Project:
    stored = library.database.put_asset(asset)
    ref = AssetRef(
        asset_id=stored.asset_id,
        source_id=None if source_record is None else source_record.source_id,
    )
    project = Project(
        content_kind="unclassified",
        state=ProjectState.INBOX,
        source_refs=(ref,),
        source_records=(() if source_record is None else (source_record,)),
        metadata={"inbox_intake_id": f"cf_intake_{uuid.uuid4().hex}"},
    )
    return library.save_project(project)


def _image_source(library, *, digit: str) -> Project:
    return _source_project(
        library,
        asset=Asset(
            sha256=digit * 64,
            media_type=MediaType.IMAGE,
            mime_type="image/png",
            size_bytes=1,
        ),
    )


def test_pr32_source_limit_is_applied_after_eligibility_filtering(tmp_path) -> None:
    app = create_app(root=tmp_path)
    try:
        source = _image_source(app.state.library, digit="1")
        # Newer unrelated rows must not consume the caller's eligible-source limit.
        for _ in range(3):
            app.state.library.save_project(Project(content_kind="note", state=ProjectState.INBOX))

        items = ProductionPresetService(app.state.library).list_sources(limit=1)
        assert len(items) == 1
        assert items[0]["source_project_id"] == source.project_id
    finally:
        app.state.runtime_lease.close()


def test_pr32_production_project_limit_is_applied_after_filtering(tmp_path) -> None:
    app = create_app(root=tmp_path)
    try:
        source = _image_source(app.state.library, digit="2")
        service = ProductionPresetService(app.state.library)
        production = service.create_project(
            request_id=str(uuid.uuid4()),
            preset_id="framed_clip",
            source_project_ids=(source.project_id,),
        )
        for _ in range(3):
            app.state.library.save_project(Project(content_kind="note", state=ProjectState.INBOX))

        projects = service.list_projects(limit=1)
        assert projects == (production,)
    finally:
        app.state.runtime_lease.close()


def test_pr32_preset_evidence_is_scalar_canonical_and_survives_review_mutation(tmp_path) -> None:
    app = create_app(root=tmp_path)
    try:
        source = _image_source(app.state.library, digit="5")
        service = ProductionPresetService(app.state.library)
        request_id = str(uuid.uuid4())
        production = service.create_project(
            request_id=request_id,
            preset_id="framed_clip",
            source_project_ids=(source.project_id,),
        )
        evidence = production.metadata["production_preset_v1"]
        assert isinstance(evidence, str)
        assert '"sources":[{"asset_id":"' in evidence
        assert f'"source_project_id":"{source.project_id}"' in evidence

        reloaded = app.state.library.load_project(production.project_id)
        assert reloaded is not None
        assert reloaded.metadata["production_preset_v1"] == evidence
        replay = service.create_project(
            request_id=request_id,
            preset_id="framed_clip",
            source_project_ids=(source.project_id,),
        )
        assert replay == reloaded

        # This crosses the historical PR10/PR17 mutation stack that previously failed on
        # nested FrozenDict metadata. Scalar evidence must remain byte-identical.
        prepared = app.state.review.bootstrap_project(production.project_id)
        assert prepared.state is ProjectState.NEEDS_REVIEW
        assert prepared.metadata["production_preset_v1"] == evidence
        assert preset_for_project(prepared) is not None
    finally:
        app.state.runtime_lease.close()


def test_pr32_exact_source_snapshot_rejects_scene_tampering_and_quarantines_catalog(tmp_path) -> None:
    app = create_app(root=tmp_path)
    try:
        first = _image_source(app.state.library, digit="6")
        second = _image_source(app.state.library, digit="7")
        service = ProductionPresetService(app.state.library)
        production = service.create_project(
            request_id=str(uuid.uuid4()),
            preset_id="panel_story",
            source_project_ids=(first.project_id, second.project_id),
        )
        payload = production.model_dump(mode="json")
        scenes = payload["scenes"]
        assert isinstance(scenes, list) and len(scenes) == 2
        first_media = scenes[0]["media"]
        scenes[0]["media"] = scenes[1]["media"]
        scenes[1]["media"] = first_media
        tampered = Project.model_validate(payload)
        app.state.library.save_project(tampered)

        with pytest.raises(ProductionPresetConflictError, match="scenes do not match"):
            preset_for_project(tampered)
        assert service.list_projects() == ()
    finally:
        app.state.runtime_lease.close()


def test_pr32_incomplete_video_probe_metadata_is_not_selectable(tmp_path) -> None:
    app = create_app(root=tmp_path)
    try:
        source = _source_project(
            app.state.library,
            asset=Asset(
                sha256="3" * 64,
                media_type=MediaType.VIDEO,
                mime_type="video/mp4",
                size_bytes=1,
                duration_seconds=4.0,
                has_audio=None,
            ),
        )
        service = ProductionPresetService(app.state.library)
        assert service.list_sources() == ()
        with pytest.raises(ProductionPresetValidationError, match="authoritative probe metadata"):
            service.create_project(
                request_id=str(uuid.uuid4()),
                preset_id="framed_clip",
                source_project_ids=(source.project_id,),
            )
    finally:
        app.state.runtime_lease.close()


def test_pr32_art_story_rejects_required_credit_without_text_before_render(tmp_path) -> None:
    app = create_app(root=tmp_path)
    try:
        asset = Asset(
            sha256="4" * 64,
            media_type=MediaType.IMAGE,
            mime_type="image/png",
            size_bytes=1,
        )
        stored = app.state.library.database.put_asset(asset)
        record = SourceRecord(
            asset_id=stored.asset_id,
            source_url="https://example.invalid/art",
            requires_credit=True,
            credit_text=None,
        )
        source = _source_project(app.state.library, asset=stored, source_record=record)
        service = ProductionPresetService(app.state.library)
        with pytest.raises(ProductionPresetValidationError, match="requires credit text"):
            service.create_project(
                request_id=str(uuid.uuid4()),
                preset_id="art_story",
                source_project_ids=(source.project_id,),
            )
    finally:
        app.state.runtime_lease.close()
