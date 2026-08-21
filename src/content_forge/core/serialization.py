"""Lossless JSON/YAML serialization for canonical Pydantic models."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)


def dump_json(model: BaseModel, *, indent: int = 2) -> str:
    """Serialize a model using standards-compliant JSON values."""

    payload = model.model_dump(mode="json")
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=indent,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )


def load_json(model_type: type[ModelT], text: str) -> ModelT:
    """Validate a model from JSON text."""

    return model_type.model_validate_json(text)


def dump_yaml(model: BaseModel) -> str:
    """Serialize a model as safe, Unicode-preserving YAML."""

    payload = model.model_dump(mode="json")
    return yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def load_yaml(model_type: type[ModelT], text: str) -> ModelT:
    """Validate a model from YAML text."""

    payload = yaml.safe_load(text)
    return model_type.model_validate(payload)


def save_model(path: str | Path, model: BaseModel) -> Path:
    """Atomically save JSON or YAML based on *path* suffix.

    Every save uses its own temporary file in the destination directory. Concurrent
    writers may still be last-writer-wins, but they cannot steal one another's temporary
    path or cause a partial destination file.
    """

    destination = Path(path)
    suffix = destination.suffix.lower()
    if suffix == ".json":
        text = dump_json(model)
    elif suffix in {".yaml", ".yml"}:
        text = dump_yaml(model)
    else:
        raise ValueError("model path must end in .json, .yaml, or .yml")

    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)

    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    return destination


def load_model(model_type: type[ModelT], path: str | Path) -> ModelT:
    """Load and validate JSON or YAML from disk."""

    source = Path(path)
    text = source.read_text(encoding="utf-8")
    suffix = source.suffix.lower()
    if suffix == ".json":
        return load_json(model_type, text)
    if suffix in {".yaml", ".yml"}:
        return load_yaml(model_type, text)
    raise ValueError("model path must end in .json, .yaml, or .yml")
