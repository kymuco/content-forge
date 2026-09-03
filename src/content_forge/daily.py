"""Persisted daily-use runtime profile for phone-first Content Forge operation."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import stat
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import uvicorn
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from content_forge.api.__main__ import (
    build_publishing_provider,
    build_tts_provider,
    validate_transport,
)
from content_forge.storage.paths import RuntimePaths, fsync_directory_chain
from content_forge.web.onboarding import normalize_public_base_url

_PROFILE_NAME = "daily-use.json"
_PROFILE_SCHEMA_VERSION = "daily-use-v1"


class DailyUseProfile(BaseModel):
    """One explicit, machine-local configuration for repeatable phone use."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["daily-use-v1"] = _PROFILE_SCHEMA_VERSION
    public_base_url: str
    host: str = "0.0.0.0"
    port: int = Field(default=8765, ge=1, le=65535)
    ssl_certfile: str
    ssl_keyfile: str
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    tts_provider: Literal["none", "qwen"] = "none"
    publishing_provider: Literal["none", "youtube"] = "none"
    youtube_token_path: str | None = None
    youtube_channel_id: str | None = None

    @field_validator("public_base_url")
    @classmethod
    def normalize_phone_url(cls, value: str) -> str:
        normalized = normalize_public_base_url(value)
        parsed = urlsplit(normalized)
        if parsed.scheme != "https":
            raise ValueError("daily-use phone URL must use HTTPS")
        if parsed.path:
            raise ValueError("daily-use phone URL must not include a path")
        return normalized

    @field_validator("host", "ssl_certfile", "ssl_keyfile", "ffmpeg_path", "ffprobe_path")
    @classmethod
    def require_nonempty_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be empty")
        return normalized

    @field_validator("youtube_token_path", "youtube_channel_id")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_runtime_contract(self) -> "DailyUseProfile":
        validate_transport(
            self.host,
            ssl_certfile=self.ssl_certfile,
            ssl_keyfile=self.ssl_keyfile,
        )
        if self.publishing_provider == "none":
            if self.youtube_token_path is not None or self.youtube_channel_id is not None:
                raise ValueError(
                    "YouTube runtime options require publishing_provider='youtube'"
                )
        else:
            if self.youtube_token_path is None:
                raise ValueError("youtube_token_path is required for YouTube publishing")
            if self.youtube_channel_id is None:
                raise ValueError("youtube_channel_id is required for YouTube publishing")
        return self

    @property
    def phone_app_url(self) -> str:
        return f"{self.public_base_url}/app/"


class DailyUseCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    ok: bool
    detail: str


def profile_path(root: str | Path | None = None) -> Path:
    return RuntimePaths.from_root(root).root / _PROFILE_NAME


def _require_regular_file(path: Path, *, private: bool) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"file does not exist: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"symlink is not accepted for daily-use file: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"not a regular file: {path}")
    if private and os.name != "nt" and metadata.st_mode & 0o077:
        raise ValueError(f"private file permissions are too broad: {path}")


def _absolute_file(value: str, *, private: bool) -> str:
    path = Path(value).expanduser()
    _require_regular_file(path, private=private)
    return str(path.resolve())


def canonicalize_profile(profile: DailyUseProfile) -> DailyUseProfile:
    """Freeze path-bearing authority to validated absolute regular files."""

    payload = profile.model_dump(mode="python")
    payload["ssl_certfile"] = _absolute_file(profile.ssl_certfile, private=False)
    payload["ssl_keyfile"] = _absolute_file(profile.ssl_keyfile, private=True)
    if profile.youtube_token_path is not None:
        payload["youtube_token_path"] = _absolute_file(
            profile.youtube_token_path,
            private=True,
        )
    return DailyUseProfile.model_validate(payload)


def save_profile(profile: DailyUseProfile, *, root: str | Path | None = None) -> Path:
    paths = RuntimePaths.from_root(root).ensure()
    target = paths.root / _PROFILE_NAME
    if target.is_symlink():
        raise ValueError("daily-use profile path must not be a symlink")

    canonical = canonicalize_profile(profile)
    encoded = (
        json.dumps(
            canonical.model_dump(mode="json"),
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    temporary = paths.root / (
        f".{_PROFILE_NAME}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        if os.name != "nt":
            os.chmod(target, 0o600)
            fsync_directory_chain(paths.root, stop_at=paths.root)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target


def load_profile(*, root: str | Path | None = None) -> DailyUseProfile:
    target = profile_path(root)
    _require_regular_file(target, private=True)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read daily-use profile: {target}") from exc
    return canonicalize_profile(DailyUseProfile.model_validate(payload))


def _executable_check(name: str, command: str) -> DailyUseCheck:
    candidate = Path(command).expanduser()
    looks_like_path = candidate.is_absolute() or candidate.parent != Path(".")
    if looks_like_path:
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            return DailyUseCheck(name=name, ok=False, detail=f"not executable: {command}")
        resolved = str(candidate.resolve())
    else:
        resolved = shutil.which(command)
    if resolved is None:
        return DailyUseCheck(name=name, ok=False, detail=f"not found: {command}")
    return DailyUseCheck(name=name, ok=True, detail=resolved)


def doctor_profile(
    profile: DailyUseProfile,
    *,
    root: str | Path | None = None,
) -> tuple[DailyUseCheck, ...]:
    checks: list[DailyUseCheck] = []
    paths = RuntimePaths.from_root(root)
    try:
        paths.ensure()
        writable_probe = paths.root / (
            f".daily-use-write-{os.getpid()}-{secrets.token_hex(8)}"
        )
        with writable_probe.open("x", encoding="utf-8"):
            pass
        writable_probe.unlink()
    except OSError as exc:
        checks.append(DailyUseCheck(name="runtime", ok=False, detail=str(exc)))
    else:
        checks.append(DailyUseCheck(name="runtime", ok=True, detail=str(paths.root.resolve())))

    for name, value, private in (
        ("tls-certificate", profile.ssl_certfile, False),
        ("tls-private-key", profile.ssl_keyfile, True),
    ):
        try:
            path = Path(_absolute_file(value, private=private))
        except ValueError as exc:
            checks.append(DailyUseCheck(name=name, ok=False, detail=str(exc)))
        else:
            checks.append(DailyUseCheck(name=name, ok=True, detail=str(path)))

    checks.append(_executable_check("ffmpeg", profile.ffmpeg_path))
    checks.append(_executable_check("ffprobe", profile.ffprobe_path))

    if profile.publishing_provider == "youtube":
        assert profile.youtube_token_path is not None
        try:
            token = Path(_absolute_file(profile.youtube_token_path, private=True))
        except ValueError as exc:
            checks.append(DailyUseCheck(name="youtube-token", ok=False, detail=str(exc)))
        else:
            checks.append(DailyUseCheck(name="youtube-token", ok=True, detail=str(token)))

    checks.append(
        DailyUseCheck(
            name="phone-url",
            ok=True,
            detail=profile.phone_app_url,
        )
    )
    return tuple(checks)


def profile_ready(checks: tuple[DailyUseCheck, ...]) -> bool:
    return all(check.ok for check in checks)


def _print_checks(checks: tuple[DailyUseCheck, ...]) -> None:
    for check in checks:
        marker = "OK" if check.ok else "FAIL"
        print(f"[{marker}] {check.name}: {check.detail}")


def _profile_from_args(args: argparse.Namespace) -> DailyUseProfile:
    return DailyUseProfile(
        public_base_url=args.phone_url,
        host=args.host,
        port=args.port,
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
        ffmpeg_path=args.ffmpeg,
        ffprobe_path=args.ffprobe,
        tts_provider=args.tts_provider,
        publishing_provider=args.publishing_provider,
        youtube_token_path=args.youtube_token,
        youtube_channel_id=args.youtube_channel_id,
    )


def _run_profile(profile: DailyUseProfile, *, root: str | Path | None) -> None:
    checks = doctor_profile(profile, root=root)
    if not profile_ready(checks):
        _print_checks(checks)
        raise ValueError("daily-use preflight failed")

    tts_provider = build_tts_provider(profile.tts_provider)
    publishing_provider = build_publishing_provider(
        profile.publishing_provider,
        youtube_token_path=profile.youtube_token_path,
        youtube_channel_id=profile.youtube_channel_id,
    )
    from content_forge.api import create_app

    app = create_app(
        root=root,
        ffmpeg_path=profile.ffmpeg_path,
        ffprobe_path=profile.ffprobe_path,
        tts_provider=tts_provider,
        publishing_provider=publishing_provider,
        public_base_url=profile.public_base_url,
    )
    print(f"Content Forge phone app: {profile.phone_app_url}")
    uvicorn.run(
        app,
        host=profile.host,
        port=profile.port,
        ssl_certfile=profile.ssl_certfile,
        ssl_keyfile=profile.ssl_keyfile,
    )


def _add_root_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        default=None,
        help="runtime root override; defaults to CONTENT_FORGE_HOME/platform data directory",
    )


def _add_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--phone-url", required=True, help="stable HTTPS base URL reachable by the phone")
    parser.add_argument("--host", default="0.0.0.0", help="bind host for the desktop worker")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ssl-certfile", required=True)
    parser.add_argument("--ssl-keyfile", required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--tts-provider", choices=("none", "qwen"), default="none")
    parser.add_argument(
        "--publishing-provider",
        choices=("none", "youtube"),
        default="none",
    )
    parser.add_argument("--youtube-token", default=None)
    parser.add_argument("--youtube-channel-id", default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Configure and run Content Forge as a repeatable phone-first daily service"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup", help="validate and persist one daily-use profile")
    _add_root_argument(setup)
    _add_runtime_arguments(setup)

    doctor = subparsers.add_parser("doctor", help="check the persisted daily-use profile")
    _add_root_argument(doctor)

    run = subparsers.add_parser("run", help="start from the persisted daily-use profile")
    _add_root_argument(run)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "setup":
            profile = canonicalize_profile(_profile_from_args(args))
            checks = doctor_profile(profile, root=args.root)
            _print_checks(checks)
            if not profile_ready(checks):
                raise ValueError("daily-use preflight failed; profile was not saved")
            target = save_profile(profile, root=args.root)
            print(f"Saved daily-use profile: {target}")
            print(f"Phone app: {profile.phone_app_url}")
            return
        profile = load_profile(root=args.root)
        if args.command == "doctor":
            checks = doctor_profile(profile, root=args.root)
            _print_checks(checks)
            if not profile_ready(checks):
                raise ValueError("daily-use preflight failed")
            return
        if args.command == "run":
            _run_profile(profile, root=args.root)
            return
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()


__all__ = [
    "DailyUseCheck",
    "DailyUseProfile",
    "build_parser",
    "canonicalize_profile",
    "doctor_profile",
    "load_profile",
    "main",
    "profile_path",
    "profile_ready",
    "save_profile",
]
