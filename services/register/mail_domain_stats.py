"""邮箱域名信誉库：按域名记录注册成败，连续被 OpenAI 拒绝的域名自动拉黑。

设计参考 grok-register-panel 的 webui/email_domain_store.py，并适配本项目
GPTMail2 动态域名池的特点：
- 选择域名时过滤已拉黑域名（冷却到期自动半开重试），池耗尽时 fail-open；
- 注册任务成功/失败通过 mark_mailbox_result 回报结果，形成闭环；
- unsupported_email（"The email you provided is not supported."）是唯一
  触发拒绝计数的确定性信号，模糊报错不计数，避免误杀好域名。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.config import DATA_DIR
from utils.log import logger

STATE_PATH = DATA_DIR / "register_domain_stats.json"
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_BLOCK_COOLDOWN_HOURS = 6.0
_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_UNSUPPORTED_MARKERS = ("unsupported_email", "the email you provided is not supported")
_lock = threading.RLock()


def _threshold() -> int:
    try:
        value = int(os.getenv("CHATGPT2API_MAIL_DOMAIN_FAILURE_THRESHOLD", ""))
        if value >= 1:
            return value
    except ValueError:
        pass
    return DEFAULT_FAILURE_THRESHOLD


def _cooldown_seconds() -> float:
    try:
        value = float(os.getenv("CHATGPT2API_MAIL_DOMAIN_BLOCK_COOLDOWN_HOURS", ""))
        if value > 0:
            return value * 3600.0
    except ValueError:
        pass
    return DEFAULT_BLOCK_COOLDOWN_HOURS * 3600.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_domain(value: object) -> str:
    text = str(value or "").strip().lower()
    if "@" in text:
        text = text.rsplit("@", 1)[-1]
    text = text.rstrip(".")
    if not text or len(text) > 253 or not _DOMAIN_RE.fullmatch(text):
        return ""
    return text


def is_domain_rejected_error(error: object) -> bool:
    """识别 OpenAI 明确的邮箱域名拒绝信号（unsupported_email）。"""
    text = str(error or "").lower()
    return any(marker in text for marker in _UNSUPPORTED_MARKERS)


def _default_item(domain: str) -> dict[str, Any]:
    return {
        "domain": domain,
        "use_count": 0,
        "success_count": 0,
        "total_rejections": 0,
        "consecutive_rejections": 0,
        "last_used_at": "",
        "last_success_at": "",
        "last_rejected_at": "",
        "last_error": "",
        "blocked_at": "",
    }


def _load_unlocked() -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for entry in items:
        if not isinstance(entry, dict):
            continue
        domain = normalize_domain(entry.get("domain"))
        if not domain:
            continue
        item = _default_item(domain)
        for key in item:
            if key in entry:
                item[key] = entry[key]
        for key in ("use_count", "success_count", "total_rejections", "consecutive_rejections"):
            try:
                item[key] = max(0, int(item[key]))
            except (TypeError, ValueError):
                item[key] = 0
        result[domain] = item
    return result


def _save_unlocked(items: dict[str, dict[str, Any]]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": _now_iso(),
        "items": sorted(items.values(), key=lambda item: item["domain"]),
    }
    tmp_path = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp_path, STATE_PATH)


def _unblock_expired_unlocked(items: dict[str, dict[str, Any]]) -> bool:
    """冷却到期的拉黑域名半开重试：清零连续拒绝计数，重新进入可用池。"""
    cooldown = _cooldown_seconds()
    now = time.time()
    changed = False
    for item in items.values():
        blocked_at = str(item.get("blocked_at") or "")
        if not blocked_at:
            continue
        try:
            blocked_ts = datetime.fromisoformat(blocked_at).timestamp()
        except ValueError:
            item["blocked_at"] = ""
            changed = True
            continue
        if now - blocked_ts >= cooldown:
            item["blocked_at"] = ""
            item["consecutive_rejections"] = 0
            item["last_error"] = f"cooldown-expired@{ _now_iso() }"
            changed = True
            logger.info({"event": "mail_domain_cooldown_expired", "domain": item["domain"]})
    return changed


def _is_blocked(item: dict[str, Any]) -> bool:
    return bool(item.get("blocked_at")) or int(item.get("consecutive_rejections") or 0) >= _threshold()


def filter_domains(provider: str, domains: list[str]) -> list[str]:
    """返回剔除已拉黑域名后的可用列表；全部被拉黑时 fail-open 返回原列表。"""
    normalized: list[str] = []
    seen: set[str] = set()
    for value in domains or []:
        domain = normalize_domain(value)
        if domain and domain not in seen:
            seen.add(domain)
            normalized.append(domain)
    if not normalized:
        return []
    with _lock:
        items = _load_unlocked()
        if items and _unblock_expired_unlocked(items):
            _save_unlocked(items)
        blocked = {
            domain
            for domain, item in items.items()
            if _is_blocked(item)
        }
    available = [domain for domain in normalized if domain not in blocked]
    if not available:
        logger.warning({
            "event": "mail_domain_pool_exhausted",
            "provider": str(provider or ""),
            "blocked_count": len(blocked),
            "total_count": len(normalized),
        })
        return normalized
    if len(available) != len(normalized):
        logger.info({
            "event": "mail_domain_filtered",
            "provider": str(provider or ""),
            "total": len(normalized),
            "available": len(available),
        })
    return available


def record_domain_result(provider: str, email_or_domain: object, outcome: str, error: object = "") -> dict[str, Any]:
    """回报某次注册对域名的结果：accepted / rejected / neutral。"""
    domain = normalize_domain(email_or_domain)
    action = str(outcome or "").strip().lower()
    if not domain or action not in {"accepted", "rejected", "neutral"}:
        return {"matched": False, "blocked": False}
    with _lock:
        items = _load_unlocked()
        item = items.setdefault(domain, _default_item(domain))
        item["use_count"] = int(item.get("use_count") or 0) + 1
        item["last_used_at"] = _now_iso()
        newly_blocked = False
        if action == "accepted":
            item["success_count"] = int(item.get("success_count") or 0) + 1
            item["consecutive_rejections"] = 0
            item["last_success_at"] = _now_iso()
            item["last_error"] = ""
            item["blocked_at"] = ""
        elif action == "rejected":
            item["total_rejections"] = int(item.get("total_rejections") or 0) + 1
            item["consecutive_rejections"] = int(item.get("consecutive_rejections") or 0) + 1
            item["last_rejected_at"] = _now_iso()
            item["last_error"] = str(error or "")[:200]
            threshold = _threshold()
            if item["consecutive_rejections"] >= threshold and not item.get("blocked_at"):
                item["blocked_at"] = _now_iso()
                newly_blocked = True
        _save_unlocked(items)
    if newly_blocked:
        logger.warning({
            "event": "mail_domain_blocked",
            "provider": str(provider or ""),
            "domain": domain,
            "consecutive_rejections": item["consecutive_rejections"],
            "error": str(error or "")[:200],
        })
    return {
        "matched": True,
        "domain": domain,
        "blocked": bool(item.get("blocked_at")),
        "newly_blocked": newly_blocked,
        "consecutive_rejections": int(item.get("consecutive_rejections") or 0),
    }


def stats_snapshot() -> dict[str, Any]:
    with _lock:
        items = _load_unlocked()
        _unblock_expired_unlocked(items)
        threshold = _threshold()
        cooldown_hours = _cooldown_seconds() / 3600.0
        entries = []
        for item in items.values():
            entries.append({
                **item,
                "blocked": _is_blocked(item),
            })
        entries.sort(key=lambda entry: (-int(entry.get("total_rejections") or 0), entry["domain"]))
        return {
            "threshold": threshold,
            "cooldown_hours": round(cooldown_hours, 2),
            "summary": {
                "total": len(entries),
                "blocked": sum(1 for entry in entries if entry["blocked"]),
                "healthy": sum(1 for entry in entries if not entry["blocked"] and int(entry.get("success_count") or 0) > 0),
            },
            "items": entries,
            "updated_at": _now_iso(),
        }


def reset_domain(domain_value: object) -> dict[str, Any]:
    domain = normalize_domain(domain_value)
    if not domain:
        return {"ok": False, "error": "域名无效"}
    with _lock:
        items = _load_unlocked()
        item = items.get(domain)
        if item is None:
            return {"ok": False, "error": "域名无记录"}
        item["consecutive_rejections"] = 0
        item["blocked_at"] = ""
        item["last_error"] = ""
        _save_unlocked(items)
    return {"ok": True, "domain": domain}
