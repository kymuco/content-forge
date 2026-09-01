from __future__ import annotations

import pytest

from content_forge.application.production_profile import (
    ProductionProfileConflictError,
    ProductionProfileDefinition,
    ProductionProfileRegistry,
    production_profile_manifest,
)
from content_forge.application.production_profile_hardening import ProductionProfileWorkflow
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


def _profile(profile_id: str, *, horizontal: bool = False) -> ProductionProfileDefinition:
    return ProductionProfileDefinition(
        profile_id=profile_id,
        scope="channel",
        display_name=profile_id,
        default_template=TemplateRef(
            template_id=(CONTENT_FRAME_TEMPLATE_ID if horizontal else HOOK_OVERLAY_TEMPLATE_ID),
            version=(INITIAL_TEMPLATE_VERSION if horizontal else HOOK_OVERLAY_TEMPLATE_VERSION),
        ),
        output_profiles=(long_form_1080p_profile() if horizontal else shorts_final_profile(),),
    )


def test_pr25_rebind_replaces_only_previous_profile_owned_defaults(tmp_path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    registries = create_builtin_registries()
    registry = ProductionProfileRegistry(library, registries)
    first = registry.put(_profile("channel_a"))
    second = registry.put(_profile("channel_b", horizontal=True))
    project = Project(content_kind="profile_fixture", state=ProjectState.DRAFT)
    library.save_project(project)

    workflow = ProductionProfileWorkflow(library, registries)
    first_manifest = workflow.bind(project.project_id, first.profile_id, revision=first.revision)
    assert first_manifest.applied_default_template is True
    assert first_manifest.applied_output_profiles is True

    second_manifest = workflow.bind(project.project_id, second.profile_id, revision=second.revision)
    current = library.load_project(project.project_id)
    assert current is not None
    assert second_manifest.revision == second
    assert second_manifest.applied_default_template is True
    assert second_manifest.applied_output_profiles is True
    assert current.template == second.definition.default_template
    assert current.output_profiles == second.definition.output_profiles
    assert production_profile_manifest(current) == second_manifest


def test_pr25_rebind_preserves_explicit_project_choices(tmp_path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    registries = create_builtin_registries()
    registry = ProductionProfileRegistry(library, registries)
    first = registry.put(_profile("channel_a"))
    second = registry.put(_profile("channel_b", horizontal=True))
    explicit_template = TemplateRef(
        template_id=CONTENT_FRAME_TEMPLATE_ID,
        version=INITIAL_TEMPLATE_VERSION,
    )
    explicit_outputs = (long_form_1080p_profile(),)
    project = Project(
        content_kind="profile_fixture",
        state=ProjectState.DRAFT,
        template=explicit_template,
        output_profiles=explicit_outputs,
    )
    library.save_project(project)

    workflow = ProductionProfileWorkflow(library, registries)
    workflow.bind(project.project_id, first.profile_id, revision=first.revision)
    rebound = workflow.bind(project.project_id, second.profile_id, revision=second.revision)
    current = library.load_project(project.project_id)
    assert current is not None
    assert current.template == explicit_template
    assert current.output_profiles == explicit_outputs
    assert rebound.applied_default_template is False
    assert rebound.applied_output_profiles is False


def test_pr25_unbind_removes_owned_defaults_and_profile_metadata(tmp_path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    registries = create_builtin_registries()
    revision = ProductionProfileRegistry(library, registries).put(_profile("channel_a"))
    project = Project(content_kind="profile_fixture", state=ProjectState.DRAFT)
    library.save_project(project)

    workflow = ProductionProfileWorkflow(library, registries)
    workflow.bind(project.project_id, revision.profile_id, revision=revision.revision)
    restored = workflow.unbind(project.project_id)

    assert restored.template is None
    assert restored.output_profiles == ()
    assert production_profile_manifest(restored) is None
    persisted = library.load_project(project.project_id)
    assert persisted == restored


def test_pr25_unbind_preserves_explicit_project_choices(tmp_path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    registries = create_builtin_registries()
    revision = ProductionProfileRegistry(library, registries).put(_profile("channel_a"))
    explicit_template = TemplateRef(
        template_id=CONTENT_FRAME_TEMPLATE_ID,
        version=INITIAL_TEMPLATE_VERSION,
    )
    explicit_outputs = (long_form_1080p_profile(),)
    project = Project(
        content_kind="profile_fixture",
        state=ProjectState.DRAFT,
        template=explicit_template,
        output_profiles=explicit_outputs,
    )
    library.save_project(project)

    workflow = ProductionProfileWorkflow(library, registries)
    workflow.bind(project.project_id, revision.profile_id, revision=revision.revision)
    restored = workflow.unbind(project.project_id)
    assert restored.template == explicit_template
    assert restored.output_profiles == explicit_outputs
    assert production_profile_manifest(restored) is None


def test_pr25_owned_drift_blocks_rebind_and_unbind(tmp_path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    registries = create_builtin_registries()
    registry = ProductionProfileRegistry(library, registries)
    first = registry.put(_profile("channel_a"))
    second = registry.put(_profile("channel_b", horizontal=True))
    project = Project(content_kind="profile_fixture", state=ProjectState.DRAFT)
    library.save_project(project)
    workflow = ProductionProfileWorkflow(library, registries)
    workflow.bind(project.project_id, first.profile_id, revision=first.revision)

    current = library.load_project(project.project_id)
    assert current is not None
    library.save_project(
        current.validated_copy(update={"output_profiles": (long_form_1080p_profile(),)})
    )

    with pytest.raises(ProductionProfileConflictError, match="output profiles drifted"):
        workflow.bind(project.project_id, second.profile_id, revision=second.revision)
    with pytest.raises(ProductionProfileConflictError, match="output profiles drifted"):
        workflow.unbind(project.project_id)


def test_pr25_same_revision_rebind_is_idempotent_with_hardened_workflow(tmp_path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    registries = create_builtin_registries()
    revision = ProductionProfileRegistry(library, registries).put(_profile("channel_a"))
    project = Project(content_kind="profile_fixture", state=ProjectState.DRAFT)
    library.save_project(project)
    workflow = ProductionProfileWorkflow(library, registries)

    first = workflow.bind(project.project_id, revision.profile_id, revision=revision.revision)
    before = library.load_project(project.project_id)
    second = workflow.bind(project.project_id, revision.profile_id, revision=revision.revision)
    after = library.load_project(project.project_id)
    assert second == first
    assert after == before
