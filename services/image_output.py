from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlparse

_RESPONSE_FORMAT_URL = "url"
_RESPONSE_FORMAT_B64 = "b64_json"
_HOST_LABEL_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-zA-Z0-9-]{1,63}(?<!-)(\.(?!-)[a-zA-Z0-9-]{1,63}(?<!-))*$")


def normalize_image_response_format(value: object | None) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in {"url", "image_url", "link"}:
        return _RESPONSE_FORMAT_URL
    if text in {"b64_json", "b64", "base64", "base64_json"}:
        return _RESPONSE_FORMAT_B64
    return None


def normalize_public_base_url(value: object | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"https://{text}"
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    # Drop path/query/fragment; image storage appends its own path.
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _host_is_ip(hostname: str) -> bool:
    host = str(hostname or "").strip().strip("[]")
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def is_public_domain_base_url(value: object | None) -> bool:
    """Accept only http(s) URLs whose host is a domain name (not an IP)."""
    normalized = normalize_public_base_url(value)
    if not normalized:
        return False
    host = urlparse(normalized).hostname or ""
    if not host or host.lower() in {"localhost"}:
        return False
    if _host_is_ip(host):
        return False
    # Allow multi-label domains and single-label internal names with letters.
    if "." not in host:
        return bool(re.fullmatch(r"[a-zA-Z][a-zA-Z0-9-]{0,62}", host))
    return bool(_HOST_LABEL_RE.fullmatch(host))


def _configured_image_generation() -> dict[str, Any]:
    from services.config import config

    raw = config.data.get("image_generation") if isinstance(getattr(config, "data", None), dict) else {}
    source = raw if isinstance(raw, dict) else {}
    output_format = normalize_image_response_format(source.get("output_format")) or _RESPONSE_FORMAT_B64
    # Compat: older configs may use output_format=url to mean URL mode.
    if str(source.get("output_format") or "").strip().lower() == "url":
        output_format = _RESPONSE_FORMAT_URL
    return {
        "enabled": bool(source.get("enabled", True)),
        "output_format": "url" if output_format == _RESPONSE_FORMAT_URL else "base64",
    }


def configured_image_url_output_enabled() -> bool:
    return _configured_image_generation().get("output_format") == "url"


def configured_image_public_base_url() -> str:
    """Domain used for public image URLs (root base_url)."""
    from services.config import config

    return normalize_public_base_url(config.base_url)


def default_image_response_format() -> str:
    """Configured default when request omits response_format."""
    if not configured_image_url_output_enabled():
        return _RESPONSE_FORMAT_B64
    if not is_public_domain_base_url(configured_image_public_base_url()):
        return _RESPONSE_FORMAT_B64
    return _RESPONSE_FORMAT_URL


def resolve_image_response_format(requested: object | None = None) -> str:
    """
    Resolve OpenAI image response_format for all image-producing endpoints.

    Priority:
    1. Explicit request value (url / b64_json)
    2. System default from settings
    3. If URL is selected but public domain is missing/invalid/IP, force b64_json
    """
    explicit = normalize_image_response_format(requested)
    fmt = explicit or default_image_response_format()
    if fmt == _RESPONSE_FORMAT_URL and not is_public_domain_base_url(configured_image_public_base_url()):
        return _RESPONSE_FORMAT_B64
    return fmt


def resolve_image_output_base_url(
    body_base_url: object | None = None,
    *,
    request_fallback: object | None = None,
    for_url_response: bool | None = None,
) -> str | None:
    """
    Resolve the public base URL used when storing/serving images.

    When URL response is active, only a non-IP domain is accepted.
    Otherwise config base_url or request host may be used as fallback.
    """
    want_url = default_image_response_format() == _RESPONSE_FORMAT_URL if for_url_response is None else bool(for_url_response)
    candidates = [
        normalize_public_base_url(body_base_url),
        configured_image_public_base_url(),
        normalize_public_base_url(request_fallback),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if want_url:
            if is_public_domain_base_url(candidate):
                return candidate
            continue
        return candidate
    return None


def validate_image_url_output_settings(settings: dict[str, Any]) -> None:
    """Raise ValueError when URL output is enabled without a valid domain base_url."""
    image_generation = settings.get("image_generation")
    source = image_generation if isinstance(image_generation, dict) else {}
    output_format = normalize_image_response_format(source.get("output_format"))
    if output_format != _RESPONSE_FORMAT_URL and str(source.get("output_format") or "").strip().lower() != "url":
        return
    base_url = normalize_public_base_url(settings.get("base_url"))
    if not base_url:
        raise ValueError("启用图片 URL 输出时，必须填写图片访问域名（base_url），且不能使用裸 IP")
    if not is_public_domain_base_url(base_url):
        raise ValueError("图片访问域名必须是 http(s) 域名，禁止使用 IP 地址或 localhost")
