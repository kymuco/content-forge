"""Loopback-only PWA onboarding helpers."""

from __future__ import annotations

import io
import ipaddress
import re
from urllib.parse import urlencode, urlsplit, urlunsplit

import segno

_SAFE_PATH = re.compile(r"^(?:/[A-Za-z0-9._~-]+)*/?$")


def _is_loopback_hostname(hostname: str | None) -> bool:
    if hostname is None:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def normalize_public_base_url(value: str) -> str:
    """Validate the phone-visible origin/base path used only for pairing navigation."""

    raw = value.strip()
    if not raw:
        raise ValueError("public_url is required")
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("public_url is invalid") from exc

    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("public_url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("public_url must not contain userinfo")
    if parsed.query or parsed.fragment:
        raise ValueError("public_url must not contain query or fragment")
    if not _SAFE_PATH.fullmatch(parsed.path or ""):
        raise ValueError("public_url path contains unsupported characters")
    if any(part in {".", ".."} for part in (parsed.path or "").split("/")):
        raise ValueError("public_url path must not traverse directories")
    if parsed.scheme == "http" and not _is_loopback_hostname(parsed.hostname):
        raise ValueError("non-loopback onboarding requires HTTPS")

    path = (parsed.path or "").rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def pairing_url(
    public_base_url: str,
    *,
    challenge_id: str,
    code: str,
) -> str:
    base = normalize_public_base_url(public_base_url)
    fragment = urlencode({"challenge_id": challenge_id, "code": code})
    return f"{base}/app/#{fragment}"


def qr_svg(value: str) -> str:
    """Render a self-contained SVG QR without contacting an external service."""

    qr = segno.make(value, error="m", micro=False)
    buffer = io.BytesIO()
    qr.save(
        buffer,
        kind="svg",
        scale=5,
        border=2,
        xmldecl=False,
        svgns=True,
    )
    return buffer.getvalue().decode("utf-8")


__all__ = ["normalize_public_base_url", "pairing_url", "qr_svg"]
