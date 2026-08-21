from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from content_forge.core import (
    AssetRef,
    AudioTrack,
    EntityKind,
    FitMode,
    NormalizedRect,
    OutputProfile,
    Overlay,
    Project,
    ProjectState,
    ReviewStatus,
    ReviewSuggestion,
    ReviewTask,
    Scene,
    SourceRecord,
    TemplateRef,
    Variant,
    new_entity_id,
)


def fixed_id(kind: EntityKind, digit: str) -> str:
    return f"cf_{kind.value}_{digit * 32}"


def build_project() -> Project:
    project_id = fixed_id(EntityKind.PROJECT, "1")
    asset_id = fixed_id(EntityKind.ASSET, "2")
    source_id = fixed_id(EntityKind.SOURCE, "3")

    source_ref = AssetRef(asset_id=asset_id, source_id=source_id)
    source = SourceRecord(
        source_id=source_id,
        asset_id=asset_id,
        source_url="https://example.invalid/source",
        platform="synthetic",
        collected_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )
    variant = Variant(
        variant_id=fixed_id(EntityKind.VARIANT, "4"),
        language="en",
        hook="This is a synthetic hook",
        title="Synthetic project",
    )
    overlay = Overlay(
        overlay_id=fixed_id(EntityKind.OVERLAY, "6"),
        component_type="text",
        variant_field="hook",
        placement=NormalizedRect(x=0.1, y=0.05, width=0.8, height=0.15),
        z_index=10,
    )
    scene = Scene(
        scene_id=fixed_id(EntityKind.SCENE, "5"),
        order=0,
        duration_seconds=5.0,
        media=source_ref,
        fit_mode=FitMode.COVER,
        overlays=(overlay,),
    )
    review = ReviewTask(
        review_task_id=fixed_id(EntityKind.REVIEW, "8"),
        project_id=project_id,
        task_type="approve_preview",
        blocking=True,
    )

    return Project(
        project_id=project_id,
        content_kind="character_moment",
        state=ProjectState.NEEDS_REVIEW,
        source_refs=(source_ref,),
        source_records=(source,),
        variants=(variant,),
        workflow_id="quick_clip",
        template=TemplateRef(template_id="hook_overlay", version="1"),
        scenes=(scene,),
        output_profiles=(
            OutputProfile(
                profile_id="preview_vertical",
                width=540,
                height=960,
                fps=30.0,
            ),
            OutputProfile(
                profile_id="short_vertical",
                width=1080,
                height=1920,
                fps=30.0,
            ),
        ),
        review_tasks=(review,),
        created_at=datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc),
    )


def test_project_contract_accepts_initial_vertical_slice() -> None:
    project = build_project()

    assert project.template is not None
    assert project.template.template_id == "hook_overlay"
    assert project.scenes[0].placement == NormalizedRect(
        x=0.0,
        y=0.0,
        width=1.0,
        height=1.0,
    )
    assert [profile.width for profile in project.output_profiles] == [540, 1080]


def test_models_are_frozen_and_validated_copy_on_write() -> None:
    project = build_project()

    with pytest.raises(ValidationError):
        project.state = ProjectState.READY  # type: ignore[misc]

    updated = project.validated_copy(update={"state": ProjectState.READY})
    assert updated.state is ProjectState.READY
    assert project.state is ProjectState.NEEDS_REVIEW


def test_nested_json_containers_cannot_mutate_canonical_state() -> None:
    project = Project(
        content_kind="synthetic",
        metadata={"nested": [{"value": 1}]},
    )

    with pytest.raises(TypeError, match="immutable"):
        project.metadata["new"] = 2  # type: ignore[index]

    nested = project.metadata["nested"]
    assert isinstance(nested, Sequence)
    with pytest.raises(TypeError, match="immutable"):
        nested.append(2)  # type: ignore[attr-defined]

    first = nested[0]
    assert isinstance(first, Mapping)
    with pytest.raises(TypeError, match="immutable"):
        first["value"] = 2  # type: ignore[index]


def test_frozen_json_containers_cannot_be_reinitialized() -> None:
    project = Project(
        content_kind="synthetic",
        metadata={"nested": [1]},
    )

    with pytest.raises(TypeError, match="immutable"):
        project.metadata.__init__({"score": float("nan")})

    nested = project.metadata["nested"]
    assert isinstance(nested, Sequence)
    with pytest.raises(TypeError, match="immutable"):
        nested.__init__([2])

    assert project.metadata == {"nested": [1]}


def test_frozen_json_containers_reject_unbound_builtin_mutators() -> None:
    project = Project(
        content_kind="synthetic",
        metadata={"nested": [1]},
    )

    with pytest.raises(TypeError):
        dict.__setitem__(project.metadata, "score", float("nan"))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        dict.update(project.metadata, {"score": float("nan")})  # type: ignore[arg-type]

    nested = project.metadata["nested"]
    assert isinstance(nested, Sequence)
    with pytest.raises(TypeError):
        list.append(nested, 2)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        list.__init__(nested, [2])  # type: ignore[arg-type]

    assert project.metadata == {"nested": [1]}


def test_validated_copy_rejects_invalid_dynamic_update() -> None:
    project = build_project()

    with pytest.raises(ValidationError):
        project.validated_copy(update={"state": "definitely_not_a_project_state"})

    with pytest.raises(ValidationError, match="timezone-aware"):
        project.validated_copy(
            update={"updated_at": datetime(2026, 8, 21, 12, 1)}
        )

    with pytest.raises(ValidationError):
        project.validated_copy(update={"unknown_field": "nope"})


def test_normalized_geometry_cannot_escape_canvas() -> None:
    with pytest.raises(ValidationError):
        NormalizedRect(x=0.8, y=0.0, width=0.3, height=1.0)


def test_non_finite_values_are_rejected_in_canonical_models() -> None:
    with pytest.raises(ValidationError):
        Project(content_kind="character_moment", metadata={"score": float("nan")})

    with pytest.raises(ValidationError):
        OutputProfile(
            profile_id="preview_vertical",
            width=540,
            height=960,
            fps=float("inf"),
        )


def test_duplicate_scene_order_is_rejected() -> None:
    project = build_project()
    duplicate_order = Scene(
        scene_id=new_entity_id(EntityKind.SCENE),
        order=0,
        duration_seconds=1.0,
    )

    with pytest.raises(ValidationError, match="duplicate scene order"):
        Project(**{**project.model_dump(), "scenes": (*project.scenes, duplicate_order)})


def test_overlay_ids_are_unique_across_scene_and_project_scope() -> None:
    project = build_project()
    duplicate = Overlay(
        overlay_id=project.scenes[0].overlays[0].overlay_id,
        component_type="image",
    )

    with pytest.raises(ValidationError, match="duplicate overlay ID across project"):
        Project(**{**project.model_dump(), "overlays": (duplicate,)})


def test_audio_ids_are_unique_across_scenes() -> None:
    duplicate_id = new_entity_id(EntityKind.AUDIO)
    first_scene = Scene(
        order=0,
        duration_seconds=1.0,
        audio_tracks=(AudioTrack(audio_track_id=duplicate_id, track_type="music"),),
    )
    second_scene = Scene(
        order=1,
        duration_seconds=1.0,
        audio_tracks=(AudioTrack(audio_track_id=duplicate_id, track_type="music"),),
    )

    with pytest.raises(ValidationError, match="duplicate audio track ID across project"):
        Project(
            content_kind="synthetic",
            scenes=(first_scene, second_scene),
        )


def test_suggestion_ids_are_unique_across_review_tasks() -> None:
    project_id = new_entity_id(EntityKind.PROJECT)
    duplicate_id = new_entity_id(EntityKind.SUGGESTION)
    suggestion_a = ReviewSuggestion(
        suggestion_id=duplicate_id,
        label="A",
        value="a",
    )
    suggestion_b = ReviewSuggestion(
        suggestion_id=duplicate_id,
        label="B",
        value="b",
    )
    first = ReviewTask(
        project_id=project_id,
        task_type="choose_hook",
        suggestions=(suggestion_a,),
    )
    second = ReviewTask(
        project_id=project_id,
        task_type="choose_title",
        suggestions=(suggestion_b,),
    )

    with pytest.raises(
        ValidationError,
        match="duplicate review suggestion ID across project",
    ):
        Project(
            project_id=project_id,
            content_kind="synthetic",
            review_tasks=(first, second),
        )


def test_review_task_must_belong_to_containing_project() -> None:
    project = build_project()
    wrong_task = ReviewTask(
        project_id=new_entity_id(EntityKind.PROJECT),
        task_type="approve_preview",
    )

    with pytest.raises(
        ValidationError,
        match="review task project_id must match containing project_id",
    ):
        Project(**{**project.model_dump(), "review_tasks": (wrong_task,)})


def test_closed_review_task_requires_resolution_timestamp() -> None:
    project_id = new_entity_id(EntityKind.PROJECT)

    with pytest.raises(ValidationError, match="requires resolved_at"):
        ReviewTask(
            project_id=project_id,
            task_type="approve_preview",
            status=ReviewStatus.RESOLVED,
        )


def test_unknown_schema_version_is_rejected() -> None:
    project = build_project()
    payload = project.model_dump(mode="json")
    payload["schema_version"] = "99.0"

    with pytest.raises(ValidationError):
        Project.model_validate(payload)
