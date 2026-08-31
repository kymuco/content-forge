from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from content_forge.audio import (
    AudioIntermediateCache,
    AudioMixPolicy,
    LoudnessMeasurement,
    apply_audio_policy,
    evaluate_audio_qc,
    loudness_apply_filter,
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
    assert resolved.audio_tracks[0].properties["fade_in_seconds"] == 0.2
    assert resolved.audio_tracks[1].gain_db == 0.0
    assert resolved.audio_tracks[1].properties["fade_in_seconds"] == 0.2
    master = resolved.output_profiles[0].properties["audio_mastering"]
    assert master["normalize"] is False
    assert master["limiter_dbfs"] == -1.5
    assert resolved.output_profiles[0].properties["audio_policy"] == {
        "policy_id": "shorts_mix",
        "version": "1.0",
    }


def test_policy_reapplication_is_stable_and_replacement_updates_owned_defaults() -> None:
    music = music_track(_ref(), duration_seconds=3.0, gain_db=-2.0)
    explicit_original = original_audio_track(
        _ref(),
        duration_seconds=1.5,
        gain_db=1.0,
        fade_in_seconds=0.05,
    )
    profile = OutputProfile(
        profile_id="preview", width=540, height=960, fps=30, audio_codec="aac"
    )
    project = Project(
        content_kind="audio_fixture",
        audio_tracks=(music, explicit_original),
        output_profiles=(profile,),
    )
    first_policy = AudioMixPolicy(
        policy_id="first",
        music_gain_db=-10.0,
        original_gain_db=-1.0,
        music_duck_db=-7.0,
        fade_in_seconds=0.2,
        fade_out_seconds=0.3,
    )
    second_policy = AudioMixPolicy(
        policy_id="second",
        music_gain_db=-6.0,
        original_gain_db=-2.0,
        music_duck_db=-4.0,
        fade_in_seconds=0.4,
        fade_out_seconds=0.5,
    )

    once = apply_audio_policy(project, first_policy)
    twice_same = apply_audio_policy(once, first_policy)
    replaced = apply_audio_policy(twice_same, second_policy)

    assert twice_same.audio_tracks[0].gain_db == -12.0
    assert twice_same.audio_tracks[1].gain_db == 0.0
    assert replaced.audio_tracks[0].gain_db == -8.0
    assert replaced.audio_tracks[0].properties["duck_db"] == -4.0
    assert replaced.audio_tracks[0].properties["fade_in_seconds"] == 0.4
    assert replaced.audio_tracks[1].gain_db == -1.0
    assert replaced.audio_tracks[1].properties["fade_in_seconds"] == 0.05
    assert replaced.audio_tracks[1].properties["fade_out_seconds"] == 0.5


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
    assert measurement.normalizable is True
    assert qc.passed is True

    quiet = evaluate_audio_qc(
        measurement.validated_copy(update={"input_i": -80.0}),
        AudioMixPolicy(),
    )
    assert quiet.silent is True
    assert quiet.passed is False


def test_silent_loudnorm_measurement_reaches_qc_but_cannot_normalize() -> None:
    stderr = """
    [Parsed_loudnorm_0 @ x] {
        "input_i" : "-inf",
        "input_tp" : "-inf",
        "input_lra" : "0.00",
        "input_thresh" : "-70.00",
        "target_offset" : "inf"
    }
    """
    measurement = parse_loudnorm_measurement(stderr)
    qc = evaluate_audio_qc(measurement, AudioMixPolicy())

    assert measurement.silent_sentinel is True
    assert measurement.normalizable is False
    assert measurement.input_i is None
    assert measurement.input_tp is None
    assert measurement.target_offset is None
    assert qc.integrated_lufs is None
    assert qc.true_peak_dbfs is None
    assert qc.silent is True
    assert qc.passed is False

    with pytest.raises(ValueError, match="cannot be used for normalization"):
        loudness_apply_filter(AudioMixPolicy(normalize=True), measurement)


def test_audio_intermediate_cache_publishes_derivation_keyed_file(tmp_path: Path) -> None:
    source = tmp_path / "master.wav"
    source.write_bytes(b"mastered-audio")
    key = "a" * 64
    cache = AudioIntermediateCache(tmp_path / "cache")

    first = cache.publish(source, key)
    second = cache.publish(source, key)

    assert first == second
    assert first.read_bytes() == b"mastered-audio"
    assert cache.has(key) is True


def test_audio_intermediate_cache_supports_concurrent_same_key_publish(tmp_path: Path) -> None:
    source = tmp_path / "premaster.wav"
    source.write_bytes(b"deterministic-premaster" * 4096)
    key = "b" * 64
    cache = AudioIntermediateCache(tmp_path / "cache")

    with ThreadPoolExecutor(max_workers=8) as pool:
        published = tuple(pool.map(lambda _: cache.publish(source, key), range(16)))

    assert len(set(published)) == 1
    assert published[0].read_bytes() == source.read_bytes()
    assert not tuple(published[0].parent.glob(published[0].name + ".tmp-*"))
