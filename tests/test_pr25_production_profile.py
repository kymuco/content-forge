from __future__ import annotations

import pytest
from pydantic import ValidationError

from content_forge.application.production_profile import (
    ProductionProfileConflictError,
    ProductionProfileDefinition,
    ProductionProfileRegistry,
    ProductionProfileValidationError,
    ProductionProfileWorkflow,
    ProfileCastDefault,
    production_profile_manifest,
)
from content_forge.application.tts import LineTTSSettings
from content_forge.application.voice_cast_models import VoiceCastDefinition
from content_forge.application.voice_cast_registry import VoiceCastRegistry
from content_forge.core import Project, ProjectState, TemplateRef
from content_forge.profiles import long_form_1080p_profile, shorts_final_profile
from content_forge.storage import LocalLibrary
from content_forge.templates import (
    CONTENT_FRAME_TEMPLATE_ID,
    HOOK_OVERLAY_TEMPLATE_ID,
    HOOK_OVERLAY_TEMPLATE_VERSION,
    INITIAL_TEMPLATE_VERSION,
    create_builtin_registries,
)


def _definition(*, display_name: str = "Channel A") -> ProductionProfileDefinition:
    return ProductionProfileDefinition(
        profile_id="channel_a",
        scope="channel",
        display_name=display_name,
        default_template=TemplateRef(
            template_id=HOOK_OVERLAY_TEMPLATE_ID,
            version=HOOK_OVERLAY_TEMPLATE_VERSION,
        ),
        default_languages=("en", "ja"),
        output_profiles=(shorts_final_profile(),),
        branding={"display_name": "Channel A"},
    )


def test_pr25_definition_rejects_duplicate_defaults() -> None:
    with pytest.raises(ValidationError, match="duplicate production profile default language"):
        ProductionProfileDefinition(
            profile_id="dup_languages",
            scope="series",
            display_name="Duplicate languages",
            default_languages=("en", "en"),
        )

    with pytest.raises(ValidationError, match="default_skin requires default_template"):
        ProductionProfileDefinition(
            profile_id="skin_without_template",
            scope="channel",
            display_name="Invalid skin",
            default_skin={"skin_id": "neutral", "version": "1.0"},
        )


def test_pr25_registry_is_revisioned_and_idempotent(tmp_path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    registry = ProductionProfileRegistry(library, create_builtin_registries())

    first = registry.put(_definition())
    same = registry.put(_definition())
    changed = registry.put(_definition(display_name="Channel A refreshed"))

    assert first.revision == 1
    assert same == first
    assert changed.revision == 2
    assert changed.definition_sha256 != first.definition_sha256
    assert registry.get("channel_a", 1) == first
    assert registry.get("channel_a") == changed


def test_pr25_registry_rejects_unknown_template_and_bad_cast_pin(tmp_path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    registries = create_builtin_registries()
    registry = ProductionProfileRegistry(library, registries)

    with pytest.raises(ProductionProfileValidationError, match="unknown template"):
        registry.put(
            ProductionProfileDefinition(
                profile_id="bad_template",
                scope="project",
                display_name="Bad template",
                default_template=TemplateRef(template_id="missing", version="1.0"),
            )
        )

    cast = VoiceCastRegistry(library).put(
        VoiceCastDefinition(
            cast_id="narrator",
            display_name="Narrator",
            settings=LineTTSSettings(voice_id="fixture_voice"),
        )
    )
    with pytest.raises(ProductionProfileConflictError, match="cast revision digest mismatch"):
        registry.put(
            ProductionProfileDefinition(
                profile_id="bad_cast",
                scope="channel",
                display_name="Bad cast",
                cast_defaults=(
                    ProfileCastDefault(
                        role="narrator",
                        cast_id=cast.cast_id,
                        cast_revision=cast.revision,
                        cast_definition_sha256="f" * 64,
                    ),
                ),
            )
        )


def test_pr25_binding_snapshots_exact_revision_and_applies_only_missing_defaults(tmp_path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    registries = create_builtin_registries()
    registry = ProductionProfileRegistry(library, registries)
    first = registry.put(_definition())

    project = Project(content_kind="profile_fixture", state=ProjectState.DRAFT)
    library.save_project(project)
    workflow = ProductionProfileWorkflow(library, registries)

    bound = workflow.bind(project.project_id, first.profile_id, revision=first.revision)
    current = library.load_project(project.project_id)
    assert current is not None
    assert current.template == first.definition.default_template
    assert current.output_profiles == first.definition.output_profiles
    assert bound.applied_default_template is True
    assert bound.applied_output_profiles is True
    assert bound.revision == first
    assert production_profile_manifest(current) == bound

    before_repeat = current
    repeated = workflow.bind(project.project_id, first.profile_id, revision=first.revision)
    after_repeat = library.load_project(project.project_id)
    assert repeated == bound
    assert after_repeat == before_repeat

    second = registry.put(_definition(display_name="New channel defaults"))
    assert second.revision == 2
    retained = workflow.manifest(project.project_id)
    assert retained is not None
    assert retained.revision == first
    assert retained.revision.definition.display_name == "Channel A"


def test_pr25_binding_preserves_explicit_project_template_and_output_profiles(tmp_path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    registries = create_builtin_registries()
    registry = ProductionProfileRegistry(library, registries)
    revision = registry.put(_definition())

    explicit_template = TemplateRef(
        template_id=CONTENT_FRAME_TEMPLATE_ID,
        version=INITIAL_TEMPLATE_VERSION,
    )
    explicit_output = long_form_1080p_profile()
    project = Project(
        content_kind="profile_fixture",
        state=ProjectState.DRAFT,
        template=explicit_template,
        output_profiles=(explicit_output,),
    )
    library.save_project(project)

    manifest = ProductionProfileWorkflow(library, registries).bind(
        project.project_id,
        revision.profile_id,
        revision=revision.revision,
    )
    current = library.load_project(project.project_id)
    assert current is not None
    assert current.template == explicit_template
    assert current.output_profiles == (explicit_output,)
    assert manifest.applied_default_template is False
    assert manifest.applied_output_profiles is False
    assert manifest.revision.definition.default_template != current.template


def test_pr25_manifest_fails_closed_if_owned_core_defaults_drift(tmp_path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    registries = create_builtin_registries()
    revision = ProductionProfileRegistry(library, registries).put(_definition())
    project = Project(content_kind="profile_fixture", state=ProjectState.DRAFT)
    library.save_project(project)
    workflow = ProductionProfileWorkflow(library, registries)
    workflow.bind(project.project_id, revision.profile_id, revision=revision.revision)

    current = library.load_project(project.project_id)
    assert current is not None
    drifted = current.validated_copy(update={"output_profiles": (long_form_1080p_profile(),)})
    library.save_project(drifted)

    with pytest.raises(ProductionProfileConflictError, match="output profiles drifted"):
        workflow.manifest(project.project_id)


def test_pr25_rebinding_to_different_revision_is_explicitly_rejected(tmp_path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    registries = create_builtin_registries()
    registry = ProductionProfileRegistry(library, registries)
    first = registry.put(_definition())
    second = registry.put(_definition(display_name="Revision two"))
    project = Project(content_kind="profile_fixture", state=ProjectState.DRAFT)
    library.save_project(project)
    workflow = ProductionProfileWorkflow(library, registries)
    workflow.bind(project.project_id, first.profile_id, revision=first.revision)

    with pytest.raises(ProductionProfileConflictError, match="explicit rebind is deferred"):
        workflow.bind(project.project_id, second.profile_id, revision=second.revision)
