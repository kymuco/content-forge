from content_forge import core
from content_forge.application import (
    ProductionProfileDefinition,
    ProductionProfileRegistry,
    ProductionProfileWorkflow,
    production_profile_manifest,
)


def test_pr25_public_application_and_language_tag_exports_are_available() -> None:
    assert ProductionProfileDefinition is not None
    assert ProductionProfileRegistry is not None
    assert ProductionProfileWorkflow is not None
    assert production_profile_manifest is not None
    assert core.LanguageTag is not None
