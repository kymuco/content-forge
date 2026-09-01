from __future__ import annotations

import wave
from pathlib import Path

import pytest

from content_forge.application import (
    CharacterCastBinding,
    LineTTSSettings,
    VoiceCastConflictError,
    VoiceCastDefinition,
    VoiceCastRegistry,
)
from content_forge.core import MediaType, dump_json
from content_forge.storage import LocalLibrary


def _reference_wav(path: Path, sample: int) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(sample.to_bytes(2, byteorder="little", signed=True) * 160)


def test_cast_revision_pins_exact_reference_audio_digest(tmp_path: Path) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    source = tmp_path / "reference-a.wav"
    _reference_wav(source, 10)
    reference = library.assets.ingest_file(
        source,
        media_type=MediaType.AUDIO,
        mime_type="audio/wav",
    ).asset
    registry = VoiceCastRegistry(library)

    revision = registry.put(
        VoiceCastDefinition(
            cast_id="clone",
            display_name="Clone",
            settings=LineTTSSettings(
                voice_id="clone-a",
                language="en",
                reference_asset_id=reference.asset_id,
                reference_text="Reference words",
            ),
        )
    )

    assert revision.reference_audio_sha256 == reference.sha256
    assert registry.get("clone", revision.revision) == revision


def test_cast_revision_rejects_valid_but_different_bytes_behind_same_asset_id(
    tmp_path: Path,
) -> None:
    library = LocalLibrary(tmp_path / "runtime")
    first_path = tmp_path / "reference-a.wav"
    second_path = tmp_path / "reference-b.wav"
    _reference_wav(first_path, 10)
    _reference_wav(second_path, 20)
    first = library.assets.ingest_file(
        first_path,
        media_type=MediaType.AUDIO,
        mime_type="audio/wav",
    ).asset
    second = library.assets.ingest_file(
        second_path,
        media_type=MediaType.AUDIO,
        mime_type="audio/wav",
    ).asset
    registry = VoiceCastRegistry(library)
    revision = registry.put(
        VoiceCastDefinition(
            cast_id="clone",
            display_name="Clone",
            settings=LineTTSSettings(
                voice_id="clone-a",
                language="en",
                reference_asset_id=first.asset_id,
                reference_text="Reference words",
            ),
        )
    )
    assert revision.reference_audio_sha256 == first.sha256

    # Simulate catalog corruption that changes the metadata lookup for the retained asset
    # ID to another valid content-addressed audio blob. A plain verify(asset) would pass;
    # the immutable cast revision must still reject the semantic substitution.
    substituted = second.validated_copy(update={"asset_id": first.asset_id})
    with library.database.transaction() as connection:
        connection.execute(
            "UPDATE assets SET manifest_json = ? WHERE asset_id = ?",
            (dump_json(substituted), first.asset_id),
        )

    with pytest.raises(VoiceCastConflictError, match="pinned content digest"):
        registry.get("clone", revision.revision)


def test_project_override_reference_requires_its_own_pinned_digest() -> None:
    settings = LineTTSSettings(
        voice_id="clone-override",
        reference_asset_id="cf_asset_00000000000000000000000000000000",
        reference_text="Reference words",
    )
    with pytest.raises(ValueError, match="override reference"):
        CharacterCastBinding(
            character_id="alice",
            cast_id="clone",
            cast_revision=1,
            cast_definition_sha256="a" * 64,
            settings_override=settings,
        )

    binding = CharacterCastBinding(
        character_id="alice",
        cast_id="clone",
        cast_revision=1,
        cast_definition_sha256="a" * 64,
        settings_override=settings,
        settings_override_reference_sha256="b" * 64,
    )
    assert binding.settings_override_reference_sha256 == "b" * 64
