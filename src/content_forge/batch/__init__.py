"""Batch preparation, recovery, QC, and reproducibility."""

from .coordinator import (
    BatchError,
    BatchIntegrityError,
    BatchPreparationError,
    BatchRenderInput,
    BatchRunError,
)
from .final import BatchCoordinator
from .models import (
    AcceptedStateSnapshot,
    BATCH_MANIFEST_VERSION,
    BATCH_RESULT_VERSION,
    BatchItemResult,
    BatchItemSnapshot,
    BatchManifest,
    BatchResultManifest,
    EXPORT_SIDECAR_VERSION,
    ExportSidecar,
    ProviderParameterSnapshot,
    QCCheckResult,
    QC_REPORT_VERSION,
    RenderQCReport,
    canonical_digest,
)
from .qc import BlackFrameAnalysis, analyze_black_frames, run_render_qc

__all__ = [
    "AcceptedStateSnapshot",
    "BATCH_MANIFEST_VERSION",
    "BATCH_RESULT_VERSION",
    "BatchCoordinator",
    "BatchError",
    "BatchIntegrityError",
    "BatchItemResult",
    "BatchItemSnapshot",
    "BatchManifest",
    "BatchPreparationError",
    "BatchRenderInput",
    "BatchResultManifest",
    "BatchRunError",
    "BlackFrameAnalysis",
    "EXPORT_SIDECAR_VERSION",
    "ExportSidecar",
    "ProviderParameterSnapshot",
    "QCCheckResult",
    "QC_REPORT_VERSION",
    "RenderQCReport",
    "analyze_black_frames",
    "canonical_digest",
    "run_render_qc",
]
