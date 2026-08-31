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
from content_forge.timeline import render_plan_digest
from content_forge.variants import (
    CompiledLanguageVariant,
    VariantCacheIdentityError,
    VariantLocalizationError,
    apply_localized_text_style,
    build_language_variant,
    compile_language_variant,
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


def test_en_ja_ko_variants_share_media_and_timeline_but_resolve_text_and_font() -> None:
    project, assets = build_multilingual_case()

    compiled = [
        compile_language_variant(
            project,
            assets,
            profile_id="preview_vertical",
            variant_id=variant.variant_id,
        )
        for variant in project.variants
    ]
    plans = [item.plan for item in compiled]

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
    assert [[overlay.properties["font"] for overlay in plan.overlays] for plan in plans] == [
        ["NotoSans", "NotoSans"],
        ["NotoSansJP", "NotoSansJP"],
        ["NotoSansKR", "NotoSansKR"],
    ]

    # Localization decorates ephemeral copies only; canonical shared timeline stays clean.
    assert all("font" not in overlay.properties for overlay in project.scenes[0].overlays)


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
    en, ja, _ = project.variants

    en_preview = compile_language_variant(
        project,
        assets,
        profile_id="preview_vertical",
        variant_id=en.variant_id,
    )
    ja_preview = compile_language_variant(
        project,
        assets,
        profile_id="preview_vertical",
        variant_id=ja.variant_id,
    )
    en_final = compile_language_variant(
        project,
        assets,
        profile_id="final_vertical",
        variant_id=en.variant_id,
    )

    first = variant_render_cache_key(en_preview, purpose="preview")
    assert variant_render_cache_key(en_preview, purpose="preview") == first
    assert variant_render_cache_key(ja_preview, purpose="preview") != first
    assert variant_render_cache_key(en_final, purpose="final") != first
    assert len(first) == 64


def test_metadata_change_requires_recompile_and_invalidates_variant_cache_identity() -> None:
    project, assets = build_multilingual_case()
    en = project.variants[0]
    compiled = compile_language_variant(
        project,
        assets,
        profile_id="preview_vertical",
        variant_id=en.variant_id,
    )
    edited = en.validated_copy(update={"title": "Edited export title"})
    edited_project = project.validated_copy(
        update={"variants": (edited, *project.variants[1:])}
    )
    edited_compiled = compile_language_variant(
        edited_project,
        assets,
        profile_id="preview_vertical",
        variant_id=edited.variant_id,
    )

    # Title is localized render/export metadata, not timeline text in this fixture.
    assert render_plan_digest(edited_compiled.plan) == render_plan_digest(compiled.plan)
    assert localized_variant_digest(edited_compiled.localized_variant) != (
        localized_variant_digest(compiled.localized_variant)
    )
    assert variant_render_cache_key(edited_compiled, purpose="preview") != (
        variant_render_cache_key(compiled, purpose="preview")
    )


def test_compiled_pair_and_cache_purpose_fail_closed_on_mismatch() -> None:
    project, assets = build_multilingual_case()
    en, ja, _ = project.variants
    compiled = compile_language_variant(
        project,
        assets,
        profile_id="preview_vertical",
        variant_id=en.variant_id,
    )

    with pytest.raises(VariantLocalizationError, match="variant_id"):
        CompiledLanguageVariant(
            plan=compiled.plan,
            localized_variant=localized_variant_snapshot(ja),
        )
    with pytest.raises(VariantCacheIdentityError, match="purpose"):
        variant_render_cache_key(compiled, purpose="final")


def test_compile_language_variant_rejects_unknown_variant() -> None:
    project, assets = build_multilingual_case()

    with pytest.raises(VariantLocalizationError, match="unknown language variant"):
        compile_language_variant(
            project,
            assets,
            profile_id="preview_vertical",
            variant_id=fixed_id(EntityKind.VARIANT, "9"),
        )
