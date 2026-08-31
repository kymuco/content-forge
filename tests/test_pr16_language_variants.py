from datetime import datetime, timezone

import pytest

from content_forge.core import (
    Asset,
    AssetRef,
    EntityKind,
    MediaType,
    NormalizedRect,
    OutputProfile,
    Overlay,
    Project,
    Scene,
)
from content_forge.timeline import compile_timeline, render_plan_digest
from content_forge.variants import (
    VariantCacheIdentityError,
    VariantLocalizationError,
    apply_localized_text_style,
    build_language_variant,
    localized_variant_digest,
    localized_variant_snapshot,
    variant_render_cache_key,
)


def fixed_id(kind: EntityKind, digit: str) -> str:
    return f"cf_{kind.value}_{digit * 32}"


def build_multilingual_case() -> tuple[Project, dict[str, Asset]]:
    image = Asset(
        asset_id=fixed_id(EntityKind.ASSET, "1"),
        sha256="1" * 64,
        media_type=MediaType.IMAGE,
        mime_type="image/png",
        size_bytes=100,
        width=1080,
        height=1920,
        storage_key="assets/shared.png",
        created_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )
    variants = (
        build_language_variant(
            variant_id=fixed_id(EntityKind.VARIANT, "2"),
            language="en",
            hook="One source, three languages",
            subtitle="The media and timing stay shared.",
            title="Shared-source example",
            description="Synthetic English metadata fixture.",
            hashtags=("#example", "#en"),
            font="NotoSans",
        ),
        build_language_variant(
            variant_id=fixed_id(EntityKind.VARIANT, "3"),
            language="ja",
            hook="一つの素材、三つの言語",
            subtitle="素材とタイミングは共有されます。",
            title="共有素材の例",
            description="合成された日本語メタデータのフィクスチャです。",
            hashtags=("#example", "#ja"),
            font="NotoSansJP",
        ),
        build_language_variant(
            variant_id=fixed_id(EntityKind.VARIANT, "4"),
            language="ko",
            hook="하나의 소스, 세 가지 언어",
            subtitle="미디어와 타이밍은 공유됩니다.",
            title="공유 소스 예시",
            description="합성 한국어 메타데이터 픽스처입니다.",
            hashtags=("#example", "#ko"),
            font="NotoSansKR",
        ),
    )
    scene = Scene(
        scene_id=fixed_id(EntityKind.SCENE, "5"),
        order=0,
        duration_seconds=3.0,
        media=AssetRef(asset_id=image.asset_id),
        overlays=(
            Overlay(
                overlay_id=fixed_id(EntityKind.OVERLAY, "6"),
                component_type="text",
                variant_field="hook",
                placement=NormalizedRect(x=0.1, y=0.1, width=0.8, height=0.15),
                z_index=10,
            ),
            Overlay(
                overlay_id=fixed_id(EntityKind.OVERLAY, "7"),
                component_type="text",
                variant_field="subtitle",
                placement=NormalizedRect(x=0.1, y=0.75, width=0.8, height=0.15),
                z_index=11,
            ),
        ),
    )
    project = Project(
        project_id=fixed_id(EntityKind.PROJECT, "8"),
        content_kind="localized_fixture",
        variants=variants,
        scenes=(scene,),
        output_profiles=(
            OutputProfile(
                profile_id="preview_vertical",
                width=540,
                height=960,
                fps=30.0,
                properties={"purpose": "preview"},
            ),
            OutputProfile(
                profile_id="final_vertical",
                width=1080,
                height=1920,
                fps=30.0,
                properties={"purpose": "final"},
            ),
        ),
        created_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )
    return project, {image.asset_id: image}


def test_en_ja_ko_variants_share_media_and_timeline_but_resolve_text() -> None:
    project, assets = build_multilingual_case()

    plans = [
        compile_timeline(
            project,
            assets,
            profile_id="preview_vertical",
            variant_id=variant.variant_id,
        )
        for variant in project.variants
    ]

    assert [plan.variant_language for plan in plans] == ["en", "ja", "ko"]
    assert [plan.scenes[0].scene_id for plan in plans] == [project.scenes[0].scene_id] * 3
    assert [plan.scenes[0].media_asset_id for plan in plans] == [
        project.scenes[0].media.asset_id
    ] * 3
    assert [[asset.asset_id for asset in plan.assets] for plan in plans] == [
        [project.scenes[0].media.asset_id]
    ] * 3
    assert [plan.total_duration_seconds for plan in plans] == [3.0, 3.0, 3.0]

    resolved = [[overlay.text for overlay in plan.overlays] for plan in plans]
    assert resolved == [
        ["One source, three languages", "The media and timing stay shared."],
        ["一つの素材、三つの言語", "素材とタイミングは共有されます。"],
        ["하나의 소스, 세 가지 언어", "미디어와 타이밍은 공유됩니다."],
    ]


def test_localized_snapshot_carries_metadata_but_no_media_or_timeline() -> None:
    project, _ = build_multilingual_case()
    snapshot = localized_variant_snapshot(project.variants[1])
    payload = snapshot.model_dump(mode="json")

    assert snapshot.language == "ja"
    assert snapshot.hook == "一つの素材、三つの言語"
    assert snapshot.subtitle == "素材とタイミングは共有されます。"
    assert snapshot.title == "共有素材の例"
    assert snapshot.description == "合成された日本語メタデータのフィクスチャです。"
    assert snapshot.hashtags == ("#example", "#ja")
    assert snapshot.font == "NotoSansJP"
    assert "scenes" not in payload
    assert "assets" not in payload
    assert "output_profiles" not in payload


def test_font_intent_is_portable_normalized_and_applies_only_when_bound() -> None:
    variant = build_language_variant(
        language="ja",
        font="  NotoSansJP  ",
        hook="synthetic",
    )

    assert localized_variant_snapshot(variant).font == "NotoSansJP"
    assert apply_localized_text_style(
        {"font": "GenericSans", "font_size": 42},
        variant=variant,
        variant_field="hook",
    ) == {"font": "NotoSansJP", "font_size": 42}
    assert apply_localized_text_style(
        {"font": "GenericSans"},
        variant=variant,
        variant_field=None,
    ) == {"font": "GenericSans"}

    with pytest.raises(VariantLocalizationError, match="filesystem path"):
        build_language_variant(language="ja", font="C:/Fonts/NotoSansJP.ttf")


def test_localization_fails_closed_on_normalized_duplicates_and_conflicts() -> None:
    with pytest.raises(VariantLocalizationError, match="localization bounds"):
        build_language_variant(
            language="en",
            hashtags=("#same", " #same "),
        )

    with pytest.raises(VariantLocalizationError, match="subtitle conflicts"):
        build_language_variant(
            language="en",
            subtitle="one",
            text_overrides={"subtitle": "two"},
        )

    with pytest.raises(VariantLocalizationError, match="font conflicts"):
        build_language_variant(
            language="en",
            font="NotoSans",
            style_overrides={"font": "NotoSerif"},
        )


def test_variant_cache_keys_are_stable_variant_specific_and_purpose_specific() -> None:
    project, assets = build_multilingual_case()
    en = project.variants[0]
    ja = project.variants[1]

    en_preview = compile_timeline(
        project,
        assets,
        profile_id="preview_vertical",
        variant_id=en.variant_id,
    )
    ja_preview = compile_timeline(
        project,
        assets,
        profile_id="preview_vertical",
        variant_id=ja.variant_id,
    )
    en_final = compile_timeline(
        project,
        assets,
        profile_id="final_vertical",
        variant_id=en.variant_id,
    )

    first = variant_render_cache_key(en_preview, en, purpose="preview")
    assert variant_render_cache_key(en_preview, en, purpose="preview") == first
    assert variant_render_cache_key(ja_preview, ja, purpose="preview") != first
    assert variant_render_cache_key(en_final, en, purpose="final") != first
    assert len(first) == 64


def test_nonrendered_metadata_change_invalidates_variant_cache_identity() -> None:
    project, assets = build_multilingual_case()
    en = project.variants[0]
    plan = compile_timeline(
        project,
        assets,
        profile_id="preview_vertical",
        variant_id=en.variant_id,
    )
    edited = en.validated_copy(update={"title": "Edited export title"})
    edited_plan = compile_timeline(
        project.validated_copy(
            update={"variants": (edited, *project.variants[1:])}
        ),
        assets,
        profile_id="preview_vertical",
        variant_id=edited.variant_id,
    )

    # Title is localized render/export metadata, not timeline text in this fixture.
    assert render_plan_digest(edited_plan) == render_plan_digest(plan)
    assert localized_variant_digest(edited) != localized_variant_digest(en)
    assert variant_render_cache_key(edited_plan, edited, purpose="preview") != (
        variant_render_cache_key(plan, en, purpose="preview")
    )


def test_cache_identity_rejects_variant_or_purpose_mismatch() -> None:
    project, assets = build_multilingual_case()
    en, ja, _ = project.variants
    plan = compile_timeline(
        project,
        assets,
        profile_id="preview_vertical",
        variant_id=en.variant_id,
    )

    with pytest.raises(VariantCacheIdentityError, match="variant_id"):
        variant_render_cache_key(plan, ja, purpose="preview")
    with pytest.raises(VariantCacheIdentityError, match="purpose"):
        variant_render_cache_key(plan, en, purpose="final")
