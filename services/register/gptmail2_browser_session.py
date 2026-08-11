"""One-shot Camoufox helper used to obtain a GPTMail browser-verification cookie.

On Linux this module is executed under ``xvfb-run`` by ``mail_provider``.  It
prints only the JSON session payload to stdout so the parent process can store
it in the runtime data directory and shut the browser down immediately.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any
from urllib.parse import unquote, urlparse


def _camoufox_proxy(value: str) -> dict[str, str] | None:
    value = str(value or "").strip()
    if not value:
        return None
    parsed = urlparse(value if "://" in value else f"http://{value}")
    if not parsed.hostname:
        raise RuntimeError("代理地址无效")
    proxy = {"server": f"{parsed.scheme or 'http'}://{parsed.hostname}"}
    if parsed.port:
        proxy["server"] += f":{parsed.port}"
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    return proxy


def _input() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def create_session(api_base: str, proxy_value: str = "") -> dict[str, str]:
    from camoufox.sync_api import Camoufox

    options: dict[str, Any] = {"headless": False}
    proxy = _camoufox_proxy(proxy_value)
    if proxy:
        options["proxy"] = proxy
    hostname = (urlparse(api_base).hostname or "").lower()

    with Camoufox(**options) as browser:
        page = browser.new_page()
        page.goto(f"{api_base.rstrip('/')}/zh/", wait_until="domcontentloaded", timeout=60_000)
        for _ in range(60):
            cookies = [
                cookie
                for cookie in page.context.cookies()
                if not hostname or hostname in str(cookie.get("domain") or "").lower()
            ]
            verified = next((cookie for cookie in cookies if cookie.get("name") == "gm_browser_verified"), None)
            if verified and str(verified.get("value") or "").strip():
                return {
                    "v": str(verified["value"]),
                    "sid": str(next((cookie.get("value") for cookie in cookies if cookie.get("name") == "gm_sid"), "") or ""),
                    "user_agent": str(page.evaluate("() => navigator.userAgent") or ""),
                }
            time.sleep(1)
    raise RuntimeError("60 秒内未获得 gm_browser_verified")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", required=True)
    args = parser.parse_args()
    options = _input()
    try:
        session = create_session(args.api_base, str(options.get("proxy") or ""))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(session, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the parent process
    raise SystemExit(main())
