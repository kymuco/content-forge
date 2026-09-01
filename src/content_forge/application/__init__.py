"""Application services between transport clients and Content Forge core/runtime."""

from .auth import AuthManager, AuthenticationError, IssuedSession
from .dialogue import (
    CharacterRecord,
    DialogueAssignment,
    DialogueAssignmentSuggestion,
    DialogueConflictError,
    DialogueError,
    DialogueLine,
    DialogueNotFoundError,
    DialogueValidationError,
    DialogueWorkflow,
    ProjectDialogueManifest,
    SceneDialogue,
    SceneFocusHint,
    dialogue_manifest,
    scene_dialogue_digest,
)
from .inbox import InboxError, InboxService, UploadTooLargeError
from .models import (
    AuthSession,
    InboxIntake,
    IntakeKind,
    IntakeState,
    PairingChallenge,
    PreparationState,
)
from .panel_ocr import (
    PanelOCRConflictError,
    PanelOCRError,
    PanelOCRNotFoundError,
    PanelOCRPreparation,
    PanelOCRValidationError,
    PanelOCRWorkflow,
    PanelTextExtraction,
    PanelTextRegion,
    panel_extraction_digest,
    prepare_panel_ocr,
)
from .repository import ApplicationRepository
from .review import (
    ReviewConflictError,
    ReviewError,
    ReviewNotFoundError,
    ReviewNotReadyError,
    ReviewRenderError,
    ReviewValidationError,
)
# Install the PR17 startup ownership boundary in place before exposing the existing
# seventh-pass ReviewService class. This preserves all public class identities.
from . import review_pr17_hardening as _review_pr17_hardening  # noqa: F401
from .review_seventh_hardening import ReviewService

__all__ = [
    "ApplicationRepository",
    "AuthManager",
    "AuthenticationError",
    "AuthSession",
    "CharacterRecord",
    "DialogueAssignment",
    "DialogueAssignmentSuggestion",
    "DialogueConflictError",
    "DialogueError",
    "DialogueLine",
    "DialogueNotFoundError",
    "DialogueValidationError",
    "DialogueWorkflow",
    "InboxError",
    "InboxIntake",
    "InboxService",
    "IntakeKind",
    "IntakeState",
    "IssuedSession",
    "PairingChallenge",
    "PanelOCRConflictError",
    "PanelOCRError",
    "PanelOCRNotFoundError",
    "PanelOCRPreparation",
    "PanelOCRValidationError",
    "PanelOCRWorkflow",
    "PanelTextExtraction",
    "PanelTextRegion",
    "PreparationState",
    "ProjectDialogueManifest",
    "ReviewConflictError",
    "ReviewError",
    "ReviewNotFoundError",
    "ReviewNotReadyError",
    "ReviewRenderError",
    "ReviewService",
    "ReviewValidationError",
    "SceneDialogue",
    "SceneFocusHint",
    "UploadTooLargeError",
    "dialogue_manifest",
    "panel_extraction_digest",
    "prepare_panel_ocr",
    "scene_dialogue_digest",
]
