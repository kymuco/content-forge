"""Local persistence and content-addressed media storage."""

from .asset_store import AssetIntegrityError, AssetStore, IngestResult, sha256_file
from .database import (
    DATABASE_SCHEMA_VERSION,
    LibraryDatabase,
    MissingAssetError,
    MissingProjectError,
    StorageConflictError,
    StorageError,
    StorageSchemaError,
)
from .job_query import list_jobs
from .job_state import transition_job_state
from .library import LocalLibrary
from .library_index import (
    LibraryDuplicateInfo,
    LibraryReuseRecord,
    LibrarySearchHit,
    LibrarySearchQuery,
    LibraryTag,
    LibraryTagKind,
    ProductionLibraryIndex,
    VirtualCollection,
)
from .paths import RuntimePaths, default_runtime_root
from .records import DerivativeSlot, SourceInput, StoredJob

__all__ = [
    "AssetIntegrityError",
    "AssetStore",
    "DATABASE_SCHEMA_VERSION",
    "DerivativeSlot",
    "IngestResult",
    "LibraryDatabase",
    "LibraryDuplicateInfo",
    "LibraryReuseRecord",
    "LibrarySearchHit",
    "LibrarySearchQuery",
    "LibraryTag",
    "LibraryTagKind",
    "LocalLibrary",
    "MissingAssetError",
    "MissingProjectError",
    "ProductionLibraryIndex",
    "RuntimePaths",
    "SourceInput",
    "StorageConflictError",
    "StorageError",
    "StorageSchemaError",
    "StoredJob",
    "VirtualCollection",
    "default_runtime_root",
    "list_jobs",
    "sha256_file",
    "transition_job_state",
]
