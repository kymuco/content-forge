from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock

import pytest
from pydantic import BaseModel

import content_forge.core.serialization as serialization_module
from content_forge.core import Project
from content_forge.core.serialization import (
    dump_json,
    dump_yaml,
    load_json,
    load_model,
    load_yaml,
    save_model,
)

from test_models import build_project


class PermissiveFloatModel(BaseModel):
    value: float


def test_json_round_trip_is_lossless() -> None:
    project = build_project()
    restored = load_json(Project, dump_json(project))

    assert restored == project


def test_yaml_round_trip_is_lossless() -> None:
    project = build_project()
    restored = load_yaml(Project, dump_yaml(project))

    assert restored == project


def test_save_and_load_json_and_yaml(tmp_path: Path) -> None:
    project = build_project()

    json_path = save_model(tmp_path / "project.json", project)
    yaml_path = save_model(tmp_path / "project.yaml", project)

    assert load_model(Project, json_path) == project
    assert load_model(Project, yaml_path) == project


def test_concurrent_saves_use_distinct_temporary_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = build_project()
    destination = tmp_path / "project.json"
    barrier = Barrier(2)
    lock = Lock()
    temporary_paths: list[Path] = []
    real_replace = serialization_module.os.replace

    def synchronized_replace(source: str | Path, target: str | Path) -> None:
        with lock:
            temporary_paths.append(Path(source))
        barrier.wait(timeout=5)
        real_replace(source, target)

    monkeypatch.setattr(serialization_module.os, "replace", synchronized_replace)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(save_model, destination, project) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)

    assert len(temporary_paths) == 2
    assert len(set(temporary_paths)) == 2
    assert load_model(Project, destination) == project
    assert list(tmp_path.glob(".project.json.*.tmp")) == []


def test_json_serializer_rejects_non_standard_nan_tokens() -> None:
    model = PermissiveFloatModel(value=float("nan"))

    with pytest.raises(ValueError):
        dump_json(model)


def test_unknown_suffix_is_rejected(tmp_path: Path) -> None:
    project = build_project()

    try:
        save_model(tmp_path / "project.txt", project)
    except ValueError as exc:
        assert ".json" in str(exc)
    else:
        raise AssertionError("unsupported suffix should fail")


def test_checked_in_example_manifest_loads() -> None:
    example = Path(__file__).parents[1] / "examples" / "minimal-project.yaml"
    project = load_model(Project, example)

    assert project.content_kind == "character_moment"
    assert project.template is not None
    assert project.template.template_id == "hook_overlay"


def test_project_json_schema_freezes_current_schema_version() -> None:
    schema = Project.model_json_schema()
    version = schema["properties"]["schema_version"]

    assert version["default"] == "1.0"
