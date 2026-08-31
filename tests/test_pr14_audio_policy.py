from __future__ import annotations

from pathlib import Path

from content_forge.audio import (
    AudioIntermediateCache,
    AudioMixPolicy,
    LoudnessMeasurement,
    apply_audio_policy,
    evaluate_audio_qc,
    music_track,
    original_audio_track,
    parse_loudnorm_measurement,
)
from content_forge.core import AssetRef, EntityKind, OutputProfile, Project, new_entity_id


def _ref() -> AssetRef:
    return AssetRef(asset_id=new_entity_id(EntityKind.ASSET))


def test_policy_materializes_track_mix_and_profile_mastering() -> None:
    music = music_track(_ref(), duration_seconds=3.0, gain_db=-2.0)
    original = original_audio_track(_ref(), duration_seconds=1.5, gain_db=1.0)
    profile = OutputProfile(
        profile_id="preview", width=540, height=960, fps=30, audio_codec="aac"
    )
    project = Project(
        content_kind="audio_fixture",
        audio_tracks=(music, original),
        output_profiles=(profile,),
    )
    policy = AudioMixPolicy(
        policy_id="shorts_mix",
        music_gain_db=-10.0,
        original_gain_db=-1.0,
        music_duck_db=-7.0,
        fade_in_seconds=0.2,
        fade_out_seconds=0.3,
        limiter_dbfs=-1.5,
    )

    resolved = apply_audio_policy(project, policy)

    assert resolved.audio_tracks[0].gain_db == -12.0
    assert resolved.audio_tracks[0].properties["duck_db"] == -7.0
    assert resolved.audio_tracks[1].gain_db == 0.0
    assert resolved.audio_tracks[1].properties["fade_in_seconds"] == 0.2
    master = resolved.output_profiles[0].properties["audio_mastering"]
    assert master["normalize"] is False
    assert master["limiter_dbfs"] == -1.5
    assert resolved.output_profiles[0].properties["audio_policy"] == {
        "policy_id": "shorts_mix",
        "version": "1.0",
    }


def test_policy_can_freeze_first_pass_measurement() -> None:
    profile = OutputProfile(
        profile_id="final", width=1080, height=1920, fps=30, audio_codec="aac"
    )
    project = Project(
        content_kind="audio_fixture",
        audio_tracks=(music_track(_ref(), duration_seconds=2.0),),
        output_profiles=(profile,),
    )
    measurement = LoudnessMeasurement(
        input_i=-20.1,
        input_tp=-4.2,
        input_lra=3.1,
        input_thresh=-30.0,
        target_offset=0.2,
    )
    resolved = apply_audio_policy(
        project,
        AudioMixPolicy(normalize=True),
        measurements={"final": measurement},
    )
    assert (
        resolved.output_profiles[0].properties["audio_mastering"]["measurement"]["input_i"]
        == -20.1
    )


def test_loudnorm_parser_and_qc_baseline() -> None:
    stderr = """
    [Parsed_loudnorm_0 @ x] {
        "input_i" : "-14.20",
        "input_tp" : "-1.10",
        "input_lra" : "4.00",
        "input_thresh" : "-24.00",
        "target_offset" : "0.10"
    }
    """
    measurement = parse_loudnorm_measurement(stderr)
    qc = evaluate_audio_qc(measurement, AudioMixPolicy())
    assert measurement.input_i == -14.2
    assert qc.passed is True

    silent = evaluate_audio_qc(
        measurement.validated_copy(update={"input_i": -80.0}),
        AudioMixPolicy(),
    )
    assert silent.silent is True
    assert silent.passed is False


def test_audio_intermediate_cache_publishes_content_addressed_file(tmp_path: Path) -> None:
    source = tmp_path / "master.wav"
    source.write_bytes(b"mastered-audio")
    key = "a" * 64
    cache = AudioIntermediateCache(tmp_path / "cache")

    first = cache.publish(source, key)
    second = cache.publish(source, key)

    assert first == second
    assert first.read_bytes() == b"mastered-audio"
    assert cache.has(key) is True
