from __future__ import annotations

import json

import pytest

from content_forge.core import (
    Asset,
    AssetRef,
    EntityKind,
    FitMode,
    MediaType,
    OutputProfile,
    Project,
    Scene,
    SourceRecord,
    TemplateRef,
    Variant,
    new_entity_id,
)
from content_forge.profiles import SHORTS_PREVIEW_PROFILE_ID, shorts_final_profile, shorts_preview_profile
from content_forge.templates import (
    ART_STORY_TEMPLATE_ID,
    CONTENT_FRAME_TEMPLATE_ID,
    HOOK_OVERLAY_TEMPLATE_ID,
    HOOK_TOPBAR_TEMPLATE_ID,
    INITIAL_TEMPLATE_IDS,
    INITIAL_TEMPLATE_VERSION,
    MEDIA_OVERLAY_COMPONENT,
    MEME_WHITE_HEADER_TEMPLATE_ID,
    PANEL_SEQUENCE_TEMPLATE_ID,
    REACTION_BOTTOM_TEMPLATE_ID,
    REGISTRY_EVIDENCE_PROPERTY,
    SOCIAL_POST_TEMPLATE_ID,
    SYNC_STACK_TEMPLATE_ID,
    InitialTemplateError,
    compile_registered_template,
    create_builtin_registries,
    initial_template_definitions,
)


def _image(*, sha: str = "a", width: int = 720, height: int = 1280) -> Asset:
    return Asset(
        asset_id=new_entity_id(EntityKind.ASSET),
        sha256=sha * 64,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
        size_bytes=123,
        width=width,
        height=height,
        has_audio=False,
    )


def _video(*, sha: str = "b", width: int = 720, height: int = 1280) -> Asset:
    return Asset(
        asset_id=new_entity_id(EntityKind.ASSET),
        sha256=sha * 64,
        media_type=MediaType.VIDEO,
        mime_type="video/mp4",
        size_bytes=456,
        width=width,
        height=height,
        duration_seconds=12.0,
        fps=30.0,
        has_audio=False,
    )


def _project(
    template_id: str,
    assets: tuple[Asset, ...],
    *,
    hook: str | None = "A compact headline stays readable",
    metadata: dict[str, object] | None = None,
    durations: tuple[float, ...] | None = None,
    source_records: tuple[SourceRecord, ...] = (),
) -> Project:
    durations = durations or tuple(2.0 for _ in assets)
    variants = () if hook is None else (Variant(language="en", hook=hook, title="Creator"),)
    return Project(
        content_kind="fixture",
        template=TemplateRef(template_id=template_id, version=INITIAL_TEMPLATE_VERSION),
        variants=variants,
        scenes=tuple(
            Scene(
                order=index,
                duration_seconds=durations[index],
                media=AssetRef(asset_id=asset.asset_id),
            )
            for index, asset in enumerate(assets)
        ),
        source_refs=tuple(AssetRef(asset_id=record.asset_id, source_id=record.source_id) for record in source_records),
        source_records=source_records,
        output_profiles=(shorts_preview_profile(), shorts_final_profile()),
        metadata=metadata or {},
    )


def _compile(project: Project, assets: tuple[Asset, ...]):
    mapping = {asset.asset_id: asset for asset in assets}
    return compile_registered_template(
        project,
        mapping,
        profile_id=SHORTS_PREVIEW_PROFILE_ID,
        variant_id=project.variants[0].variant_id if project.variants else None,
    )


def _evidence(plan) -> dict[str, object]:
    raw = plan.template_properties[REGISTRY_EVIDENCE_PROPERTY]
    assert isinstance(raw, str)
    return json.loads(raw)


def test_builtin_registry_contains_complete_initial_pack_and_generic_overlay_component() -> None:
    bundle = create_builtin_registries()
    identities = {(item.template_id, item.version) for item in bundle.templates.definitions()}

    assert (HOOK_OVERLAY_TEMPLATE_ID, "1.0") in identities
    assert {(template_id, INITIAL_TEMPLATE_VERSION) for template_id in INITIAL_TEMPLATE_IDS} <= identities
    assert len(INITIAL_TEMPLATE_IDS) == 8
    assert bundle.components.get("media_overlay", "1.0") == MEDIA_OVERLAY_COMPONENT
    assert MEDIA_OVERLAY_COMPONENT.output_kind == "overlay"
    assert MEDIA_OVERLAY_COMPONENT.accepts_asset is True


def test_initial_definitions_are_declarative_and_renderer_independent() -> None:
    definitions = initial_template_definitions()

    assert tuple(item.template_id for item in definitions) == INITIAL_TEMPLATE_IDS
    assert all(item.version == INITIAL_TEMPLATE_VERSION for item in definitions)
    assert all(item.metadata["renderer_specific"] is False for item in definitions)
    content_frame = next(item for item in definitions if item.template_id == CONTENT_FRAME_TEMPLATE_ID)
    assert content_frame.metadata["anime_frame_use_case"] is True


@pytest.mark.parametrize(
    "template_id",
    [
        HOOK_TOPBAR_TEMPLATE_ID,
        SOCIAL_POST_TEMPLATE_ID,
        MEME_WHITE_HEADER_TEMPLATE_ID,
        CONTENT_FRAME_TEMPLATE_ID,
    ],
)
def test_single_media_text_templates_compile_with_exact_registry_evidence(template_id: str) -> None:
    asset = _image()
    metadata = {"social_post.display_name": "Forge", "social_post.handle": "@forge"}
    project = _project(template_id, (asset,), metadata=metadata)

    plan = _compile(project, (asset,))
    evidence = _evidence(plan)

    assert plan.template_id == template_id
    assert plan.template_version == INITIAL_TEMPLATE_VERSION
    assert evidence["template"]["template_id"] == template_id
    assert evidence["template"]["version"] == INITIAL_TEMPLATE_VERSION
    assert len(evidence["template"]["definition_sha256"]) == 64
    assert plan.scenes[0].media_asset_id == asset.asset_id


def test_hook_topbar_moves_media_below_dedicated_header() -> None:
    asset = _image()
    plan = _compile(_project(HOOK_TOPBAR_TEMPLATE_ID, (asset,)), (asset,))

    assert plan.scenes[0].placement.y == pytest.approx(0.22)
    assert plan.scenes[0].placement.height == pytest.approx(0.78)
    assert plan.overlays[0].text == "A compact\nheadline stays\nreadable"
    assert plan.overlays[0].placement.y == pytest.approx(0.06)


def test_social_post_uses_explicit_identity_metadata_without_changing_project_schema() -> None:
    asset = _image()
    project = _project(
        SOCIAL_POST_TEMPLATE_ID,
        (asset,),
        metadata={
            "social_post.display_name": "Content Forge",
            "social_post.handle": "@forge",
        },
    )

    plan = _compile(project, (asset,))

    assert plan.overlays[0].text is not None
    assert "Content Forge @forge" in plan.overlays[0].text
    assert "A compact headline" in plan.overlays[0].text
    assert plan.overlays[0].properties["box"] is True


def test_meme_white_header_is_bounded_white_text_box_over_media_layout() -> None:
    asset = _image()
    plan = _compile(_project(MEME_WHITE_HEADER_TEMPLATE_ID, (asset,)), (asset,))

    overlay = plan.overlays[0]
    assert overlay.properties["font_color"] == "black"
    assert overlay.properties["box"] is True
    assert overlay.properties["box_color"] == "white"
    assert plan.template_properties["white_header_mode"] == "bounded_drawtext_box_v1"
    assert plan.scenes[0].placement.y == pytest.approx(0.28)


def test_content_frame_can_represent_anime_frame_use_case_without_an_anime_specific_id() -> None:
    asset = _image(width=1280, height=720)
    plan = _compile(_project(CONTENT_FRAME_TEMPLATE_ID, (asset,)), (asset,))

    assert plan.scenes[0].fit_mode is FitMode.CONTAIN
    assert plan.scenes[0].placement.x == pytest.approx(0.06)
    assert plan.scenes[0].placement.width == pytest.approx(0.78)
    assert plan.template_id == CONTENT_FRAME_TEMPLATE_ID


def test_content_frame_remains_valid_without_optional_text_variant() -> None:
    asset = _image()
    project = _project(CONTENT_FRAME_TEMPLATE_ID, (asset,), hook=None)

    plan = _compile(project, (asset,))

    assert plan.variant_id is None
    assert plan.overlays == ()


def test_art_story_preserves_order_and_durations_and_emits_source_credit() -> None:
    first = _image(sha="a")
    second = _image(sha="b", width=1280, height=720)
    record = SourceRecord(
        asset_id=first.asset_id,
        credit_text="Artist: fixture",
        requires_credit=True,
    )
    project = _project(
        ART_STORY_TEMPLATE_ID,
        (first, second),
        hook=None,
        durations=(2.5, 3.25),
        source_records=(record,),
    )

    plan = _compile(project, (first, second))

    assert [scene.order for scene in plan.scenes] == [0, 1]
    assert [scene.duration_seconds for scene in plan.scenes] == [2.5, 3.25]
    assert all(scene.fit_mode is FitMode.CONTAIN for scene in plan.scenes)
    assert plan.template_properties["sequence_length"] == 2
    assert plan.overlays[0].text == "Artist: fixture"


def test_art_story_and_panel_sequence_fail_closed_on_video_media() -> None:
    video = _video()
    for template_id in (ART_STORY_TEMPLATE_ID, PANEL_SEQUENCE_TEMPLATE_ID):
        project = _project(template_id, (video,), hook=None)
        with pytest.raises(InitialTemplateError, match="requires image media"):
            _compile(project, (video,))


def test_panel_sequence_uses_canonical_project_timing_and_readable_contain_geometry() -> None:
    first = _image(sha="a")
    second = _image(sha="b")
    third = _image(sha="c")
    project = _project(
        PANEL_SEQUENCE_TEMPLATE_ID,
        (first, second, third),
        hook=None,
        durations=(1.5, 2.0, 3.0),
    )

    plan = _compile(project, (first, second, third))

    assert [scene.duration_seconds for scene in plan.scenes] == [1.5, 2.0, 3.0]
    assert all(scene.fit_mode is FitMode.CONTAIN for scene in plan.scenes)
    assert plan.template_properties["pacing"] == "project_scene_durations"
    assert plan.template_properties["sequence_length"] == 3


def test_sync_stack_builds_three_aspect_safe_synchronized_copies_without_renderer_branch() -> None:
    asset = _image(width=1280, height=720)
    project = _project(
        SYNC_STACK_TEMPLATE_ID,
        (asset,),
        metadata={"sync_stack.copies": 3},
    )

    plan = _compile(project, (asset,))

    assert plan.template_properties["copies"] == 3
    assert len(plan.scenes) == 1
    assert len([item for item in plan.overlays if item.asset_id == asset.asset_id]) == 2
    assert plan.scenes[0].fit_mode is FitMode.CONTAIN
    rects = plan.template_properties["aspect_safe_rects"]
    assert len(rects) == 3
    source_aspect = asset.width / asset.height
    for rect in rects:
        pixel_aspect = (rect["width"] * 540) / (rect["height"] * 960)
        assert pixel_aspect == pytest.approx(source_aspect)


def test_sync_stack_rejects_unbounded_copy_count_and_missing_dimensions() -> None:
    asset = _image()
    project = _project(
        SYNC_STACK_TEMPLATE_ID,
        (asset,),
        metadata={"sync_stack.copies": 4},
    )
    with pytest.raises(InitialTemplateError, match="integer 2 or 3"):
        _compile(project, (asset,))

    unknown = asset.validated_copy(update={"width": None, "height": None})
    project = _project(SYNC_STACK_TEMPLATE_ID, (unknown,))
    with pytest.raises(InitialTemplateError, match="dimensions are required"):
        _compile(project, (unknown,))


def test_profile_derived_overlay_templates_reject_preview_final_aspect_drift() -> None:
    asset = _image(width=1280, height=720)
    square = OutputProfile(profile_id="square", width=720, height=720, fps=30)
    for template_id, metadata in (
        (SYNC_STACK_TEMPLATE_ID, {"sync_stack.copies": 2}),
        (REACTION_BOTTOM_TEMPLATE_ID, {"reaction_bottom.reaction_asset_id": _image(sha="d").asset_id}),
    ):
        project = _project(template_id, (asset,), hook=None, metadata=metadata)
        project = project.validated_copy(
            update={"output_profiles": (shorts_preview_profile(), square)}
        )
        extra = _image(sha="d") if template_id == REACTION_BOTTOM_TEMPLATE_ID else None
        assets = (asset,) if extra is None else (asset, extra)
        if extra is not None:
            project = project.validated_copy(
                update={"metadata": {"reaction_bottom.reaction_asset_id": extra.asset_id}}
            )
        with pytest.raises(InitialTemplateError, match="share one canvas aspect ratio"):
            _compile(project, assets)


def test_reaction_bottom_requires_distinct_stable_reaction_asset_and_preserves_aspect() -> None:
    main = _image(sha="a", width=720, height=1280)
    reaction = _image(sha="b", width=640, height=360)
    project = _project(
        REACTION_BOTTOM_TEMPLATE_ID,
        (main,),
        hook=None,
        metadata={"reaction_bottom.reaction_asset_id": reaction.asset_id},
    )

    plan = _compile(project, (main, reaction))

    assert plan.template_properties["reaction_asset_id"] == reaction.asset_id
    assert plan.overlays[0].asset_id == reaction.asset_id
    rect = plan.template_properties["reaction_rect"]
    pixel_aspect = (rect["width"] * 540) / (rect["height"] * 960)
    assert pixel_aspect == pytest.approx(reaction.width / reaction.height)
    evidence = _evidence(plan)
    assert ("media_overlay", "1.0") in {
        (item["component_id"], item["version"]) for item in evidence["components"]
    }


def test_reaction_bottom_preserves_unambiguous_source_lineage() -> None:
    main = _image(sha="a")
    reaction = _image(sha="b", width=640, height=360)
    record = SourceRecord(asset_id=reaction.asset_id, source_url="https://example.test/reaction")
    project = _project(
        REACTION_BOTTOM_TEMPLATE_ID,
        (main,),
        hook=None,
        metadata={"reaction_bottom.reaction_asset_id": reaction.asset_id},
        source_records=(record,),
    )

    plan = _compile(project, (main, reaction))

    assert plan.overlays[0].source_id == record.source_id


def test_reaction_bottom_rejects_ambiguous_source_lineage() -> None:
    main = _image(sha="a")
    reaction = _image(sha="b", width=640, height=360)
    first = SourceRecord(asset_id=reaction.asset_id, source_url="https://example.test/one")
    second = SourceRecord(asset_id=reaction.asset_id, source_url="https://example.test/two")
    project = _project(
        REACTION_BOTTOM_TEMPLATE_ID,
        (main,),
        hook=None,
        metadata={"reaction_bottom.reaction_asset_id": reaction.asset_id},
        source_records=(first, second),
    )

    with pytest.raises(InitialTemplateError, match="ambiguous project source provenance"):
        _compile(project, (main, reaction))


def test_reaction_bottom_rejects_missing_invalid_or_self_reaction_identity() -> None:
    main = _image()
    missing = _project(REACTION_BOTTOM_TEMPLATE_ID, (main,), hook=None)
    with pytest.raises(InitialTemplateError, match="reaction_asset_id"):
        _compile(missing, (main,))

    invalid = _project(
        REACTION_BOTTOM_TEMPLATE_ID,
        (main,),
        hook=None,
        metadata={"reaction_bottom.reaction_asset_id": "/tmp/reaction.png"},
    )
    with pytest.raises(InitialTemplateError, match="Content Forge asset ID"):
        _compile(invalid, (main,))

    same = _project(
        REACTION_BOTTOM_TEMPLATE_ID,
        (main,),
        hook=None,
        metadata={"reaction_bottom.reaction_asset_id": main.asset_id},
    )
    with pytest.raises(InitialTemplateError, match="distinct"):
        _compile(same, (main,))
