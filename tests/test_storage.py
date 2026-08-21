from pathlib import Path

import pytest
from pydantic import ValidationError

from content_forge.core import AssetRef, EntityKind, MediaType, Project, new_entity_id
from content_forge.storage import (
    AssetIntegrityError,
    DerivativeSlot,
    LocalLibrary,
    MissingAssetError,
    SourceInput,
    StoredJob,
)


def write_fixture(path: Path, payload: bytes = b"synthetic-media-bytes") -> Path:
    path.write_bytes(payload)
    return path


def test_duplicate_ingest_reuses_one_asset_and_preserves_provenance(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    first_file = write_fixture(tmp_path / "first.mp4")
    second_file = write_fixture(tmp_path / "second.mp4")

    first = library.assets.ingest_file(
        first_file,
        source=SourceInput(
            source_url="https://example.invalid/post/1",
            platform="reddit",
            creator_handle="@creator_a",
            original_title="First source",
        ),
    )
    second = library.assets.ingest_file(
        second_file,
        source=SourceInput(
            source_url="https://example.invalid/post/2",
            platform="mirror",
            creator_handle="@creator_b",
            original_title="Second source",
        ),
    )

    assert first.asset.asset_id == second.asset.asset_id
    assert first.asset.sha256 == second.asset.sha256
    assert first.deduplicated is False
    assert second.deduplicated is True
    assert first.asset.media_type is MediaType.VIDEO
    assert first.blob_path == second.blob_path
    assert first.blob_path.read_bytes() == b"synthetic-media-bytes"

    sources = library.database.list_sources(first.asset.asset_id)
    assert len(sources) == 2
    assert {record.source_url for record in sources} == {
        "https://example.invalid/post/1",
        "https://example.invalid/post/2",
    }


def test_blob_layout_is_content_addressed_and_verifiable(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    result = library.assets.ingest_file(write_fixture(tmp_path / "image.png", b"image"))

    digest = result.asset.sha256
    assert result.asset.storage_key == (
        f"assets/sha256/{digest[:2]}/{digest[2:4]}/{digest}"
    )
    assert library.assets.resolve(result.asset) == result.blob_path
    assert library.assets.verify(result.asset) is True


def test_blob_corruption_is_detected(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    fixture = write_fixture(tmp_path / "clip.mp4", b"original")
    result = library.assets.ingest_file(fixture)
    result.blob_path.write_bytes(b"corrupt")

    with pytest.raises(AssetIntegrityError):
        library.assets.ingest_file(fixture)


def test_project_round_trip_references_library_asset_and_provenance(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    ingest = library.assets.ingest_file(
        write_fixture(tmp_path / "panel.png", b"panel"),
        source=SourceInput(
            source_url="https://example.invalid/artist/post",
            platform="artist_site",
            creator_handle="@artist",
            credit_text="Art by @artist",
            requires_credit=True,
        ),
    )
    assert ingest.source_record is not None

    ref = AssetRef(
        asset_id=ingest.asset.asset_id,
        source_id=ingest.source_record.source_id,
    )
    project = Project(
        content_kind="art_story",
        source_refs=(ref,),
        source_records=(ingest.source_record,),
    )

    library.save_project(project)
    restored = library.load_project(project.project_id)

    assert restored == project
    assert library.database.project_ids_for_asset(ingest.asset.asset_id) == (
        project.project_id,
    )


def test_multiple_projects_can_reference_the_same_asset(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    ingest = library.assets.ingest_file(write_fixture(tmp_path / "clip.mp4"))
    ref = AssetRef(asset_id=ingest.asset.asset_id)
    first = Project(content_kind="character_moment", source_refs=(ref,))
    second = Project(content_kind="sync_meme", source_refs=(ref,))

    library.save_project(first)
    library.save_project(second)

    assert library.database.project_ids_for_asset(ingest.asset.asset_id) == tuple(
        sorted((first.project_id, second.project_id))
    )


def test_project_cannot_persist_dangling_asset_reference(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    ref = AssetRef(asset_id=new_entity_id(EntityKind.ASSET))
    project = Project(content_kind="synthetic", source_refs=(ref,))

    with pytest.raises(MissingAssetError):
        library.save_project(project)


def test_derivative_metadata_slots_are_persistent(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    ingest = library.assets.ingest_file(write_fixture(tmp_path / "clip.mp4"))
    slot = DerivativeSlot(
        asset_id=ingest.asset.asset_id,
        slot="thumbnail.primary",
        storage_key="derived/thumb.webp",
        metadata={"width": 320, "height": 180},
    )

    library.database.put_derivative_slot(slot)
    restored = library.database.get_derivative_slot(
        ingest.asset.asset_id, "thumbnail.primary"
    )

    assert restored == slot


def test_job_metadata_is_persistent_and_uses_job_ids(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    project = Project(content_kind="synthetic")
    library.save_project(project)
    job = StoredJob(
        project_id=project.project_id,
        job_type="render.preview",
        payload={"profile": "preview_vertical"},
    )

    library.database.create_job(job)
    queued = library.database.get_job(job.job_id)
    assert queued == job
    assert job.job_id.startswith("cf_job_")

    running = library.database.update_job_state(job.job_id, "running")
    assert running.state == "running"
    assert running.updated_at >= job.updated_at


def test_job_state_update_is_validated_before_persistence(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    job = StoredJob(job_type="render.preview")
    library.database.create_job(job)

    with pytest.raises(ValidationError):
        library.database.update_job_state(job.job_id, "NOT A REGISTRY KEY")

    restored = library.database.get_job(job.job_id)
    assert restored == job
