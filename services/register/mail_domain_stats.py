"""邮箱域名信誉库：按域名记录注册成败，自动拉黑被拒绝/收不到验证码的域名。

设计参考 grok-register-panel 的 webui/email_domain_store.py，并适配本项目
GPTMail2 动态域名池的特点：

- 选择域名时过滤已拉黑域名（冷却到期自动半开重试），池耗尽时 fail-open；
- 注册任务成功/失败通过 mark_mailbox_result 回报结果，形成闭环；
- unsupported_email（"The email you provided is not supported."）是确定性拒绝
  信号，首次出现即拉黑；
- 验证码收不到（OTP 超时）是 OpenAI/GPTMail2 对域名静默丢信的弱信号：实测
  1410 个免费域名里约 1061 个从未送达（0% 到达率），另 185 个稳定送达，
  因此对静默丢信采用"连续 N 次无信且从未收到过验证码"才拉黑，冷却期更长；
- 有送达记录的域名进入 proven 池，选择时按比例优先复用（explore ratio 用于
  持续发现新的可用域名），把选择命中率从 ~15% 提升到 ~90% 量级。
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.config import DATA_DIR
from utils.log import logger

STATE_PATH = DATA_DIR / "register_domain_stats.json"
# unsupported_email 是 OpenAI 对域名的确定性拒绝信号，且本项目主要消费 GPTMail2
# 免费轮换域名池（域名可弃、冷却后自动半开重试），首次拒绝即拉黑是最优策略。
DEFAULT_FAILURE_THRESHOLD = 1
DEFAULT_BLOCK_COOLDOWN_HOURS = 6.0
# 静默丢信（OTP 超时）是弱信号，需要连续多次确认才拉黑。
DEFAULT_SILENT_FAILURE_THRESHOLD = 2
DEFAULT_SILENT_BLOCK_COOLDOWN_HOURS = 24.0
# proven 池少于该值时，优先继续探索未知域名，避免过早锁死在小样本上。
DEFAULT_PROVEN_POOL_MIN = 5
# proven 池足够大时，保留该比例的随机探索预算用于发现新的可用域名。
DEFAULT_EXPLORE_RATIO = 0.15
# 未知域名少于该值时，探索没有意义（会反复撞同一个域名），直接全程用 proven。
DEFAULT_EXPLORE_MIN_POOL = 3
# 域名被上游邮箱服务限流（GPTMail2 inbox_request_rate_limited）后的冷却时长。
DEFAULT_THROTTLE_COOLDOWN_SECONDS = 120.0
_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_UNSUPPORTED_MARKERS = ("unsupported_email", "the email you provided is not supported")
_CODE_MISSING_MARKERS = ("等待注册验证码超时", "等待 microsoft 登录验证码超时", "验证码超时")
_THROTTLE_MARKERS = ("inbox_request_rate_limited", "too many inbox requests", "http 429")
_OUTCOMES = frozenset({"accepted", "rejected", "neutral", "delivered", "no_code", "throttled"})
_lock = threading.RLock()


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, ""))
        if value >= 0:
            return value
    except ValueError:
        pass
    return default


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, ""))
        if value > 0:
            return value
    except ValueError:
        pass
    return default


def _threshold() -> int:
    return max(1, _env_int("CHATGPT2API_MAIL_DOMAIN_FAILURE_THRESHOLD", DEFAULT_FAILURE_THRESHOLD))


def _silent_threshold() -> int:
    return max(1, _env_int("CHATGPT2API_MAIL_DOMAIN_SILENT_THRESHOLD", DEFAULT_SILENT_FAILURE_THRESHOLD))


def _cooldown_seconds() -> float:
    return _env_float("CHATGPT2API_MAIL_DOMAIN_BLOCK_COOLDOWN_HOURS", DEFAULT_BLOCK_COOLDOWN_HOURS) * 3600.0


def _silent_cooldown_seconds() -> float:
    return _env_float("CHATGPT2API_MAIL_DOMAIN_SILENT_COOLDOWN_HOURS", DEFAULT_SILENT_BLOCK_COOLDOWN_HOURS) * 3600.0


def _proven_pool_min() -> int:
    return max(1, _env_int("CHATGPT2API_MAIL_DOMAIN_PROVEN_POOL_MIN", DEFAULT_PROVEN_POOL_MIN))


def _explore_ratio() -> float:
    return min(1.0, _env_float("CHATGPT2API_MAIL_DOMAIN_EXPLORE_RATIO", DEFAULT_EXPLORE_RATIO))


def _explore_min_pool() -> int:
    return max(1, _env_int("CHATGPT2API_MAIL_DOMAIN_EXPLORE_MIN_POOL", DEFAULT_EXPLORE_MIN_POOL))


def _throttle_cooldown_seconds() -> float:
    return _env_float("CHATGPT2API_MAIL_DOMAIN_THROTTLE_COOLDOWN_SECONDS", DEFAULT_THROTTLE_COOLDOWN_SECONDS)


def _now_iso() -> str:
    # 毫秒精度：限流短冷却（秒级）需要更细的时间戳，秒级截断会让刚记录的限流立即过期。
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


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


def is_code_missing_error(error: object) -> bool:
    """识别"邮箱收不到验证码"这一类失败（域名静默丢信的弱信号）。"""
    text = str(error or "")
    if not text:
        return False
    if is_domain_rejected_error(text):
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _CODE_MISSING_MARKERS)


def is_domain_throttled_error(error: object) -> bool:
    """识别上游邮箱服务对某域名的限流（GPTMail2 inbox_request_rate_limited）。"""
    text = str(error or "").lower()
    return any(marker in text for marker in _THROTTLE_MARKERS)


def _default_item(domain: str) -> dict[str, Any]:
    return {
        "domain": domain,
        "use_count": 0,
        "success_count": 0,
        "total_rejections": 0,
        "consecutive_rejections": 0,
        "code_received_count": 0,
        "no_code_count": 0,
        "consecutive_no_code": 0,
        "last_used_at": "",
        "last_success_at": "",
        "last_rejected_at": "",
        "last_code_at": "",
        "last_no_code_at": "",
        "last_error": "",
        "blocked_at": "",
        "silent_blocked_at": "",
        "throttled_at": "",
        "throttle_count": 0,
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
        for key in (
            "use_count",
            "success_count",
            "total_rejections",
            "consecutive_rejections",
            "code_received_count",
            "no_code_count",
            "consecutive_no_code",
            "throttle_count",
        ):
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


def _expired(blocked_at: str, cooldown: float, now: float) -> bool | None:
    """返回 True=已过期，False=仍在冷却，None=时间戳损坏（按过期处理）。"""
    if not blocked_at:
        return False
    try:
        return now - datetime.fromisoformat(blocked_at).timestamp() >= cooldown
    except ValueError:
        return None


def _unblock_expired_unlocked(items: dict[str, dict[str, Any]]) -> bool:
    """冷却到期的拉黑域名半开重试：清零连续计数，重新进入可用池。"""
    cooldown = _cooldown_seconds()
    silent_cooldown = _silent_cooldown_seconds()
    now = time.time()
    changed = False
    for item in items.values():
        blocked_at = str(item.get("blocked_at") or "")
        if blocked_at:
            expired = _expired(blocked_at, cooldown, now)
            if expired is not False:
                item["blocked_at"] = ""
                item["consecutive_rejections"] = 0
                item["last_error"] = f"cooldown-expired@{_now_iso()}"
                changed = True
                logger.info({"event": "mail_domain_cooldown_expired", "domain": item["domain"]})
        silent_blocked_at = str(item.get("silent_blocked_at") or "")
        if silent_blocked_at:
            expired = _expired(silent_blocked_at, silent_cooldown, now)
            if expired is not False:
                item["silent_blocked_at"] = ""
                item["consecutive_no_code"] = 0
                item["last_error"] = f"silent-cooldown-expired@{_now_iso()}"
                changed = True
                logger.info({"event": "mail_domain_silent_cooldown_expired", "domain": item["domain"]})
    return changed


def _is_blocked(item: dict[str, Any]) -> bool:
    return bool(
        item.get("blocked_at")
        or item.get("silent_blocked_at")
        or int(item.get("consecutive_rejections") or 0) >= _threshold()
    )


def _proven(item: dict[str, Any] | None) -> bool:
    return bool(item) and int(item.get("code_received_count") or 0) > 0


def _should_silent_block(item: dict[str, Any]) -> bool:
    """是否应把该域名按"静默丢信"拉黑。

    - 从未送达过验证码是前提（送达过的域名不会被静默拉黑）；
    - 连续丢信达到阈值即拉黑；
    - 历史累计丢信已达阈值时，冷却结束后的第一次丢信立即重新拉黑：坏域名池
      （实测约 75% 的免费域名）每 24 小时回来一次、每次都要烧掉 80 秒等待，
      没必要重新取证两次。
    """
    if int(item.get("code_received_count") or 0) > 0:
        return False
    threshold = _silent_threshold()
    return (
        int(item.get("consecutive_no_code") or 0) >= threshold
        or int(item.get("no_code_count") or 0) >= threshold
    )


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


def select_domain(provider: str, domains: list[str]) -> str:
    """在可用域名中挑一个：优先已证明能收信的域名，并保留少量探索预算。

    分层：
    - proven：收到过验证码，命中率最高；
    - unknown：从未试过，需要探索（仅在数量足够时探索，否则会反复撞同一个域名）；
    - risky：试过但没收到信（未达拉黑阈值），最后才用。
    被上游限流（inbox_request_rate_limited）的域名在冷却期内跳过。
    """
    available = filter_domains(provider, domains)
    if not available:
        return ""
    with _lock:
        items = _load_unlocked()
    throttle_cooldown = _throttle_cooldown_seconds()
    now_ts = time.time()

    def is_throttled(domain: str) -> bool:
        raw = str((items.get(domain) or {}).get("throttled_at") or "")
        if not raw:
            return False
        try:
            return now_ts - datetime.fromisoformat(raw).timestamp() < throttle_cooldown
        except ValueError:
            return False

    fresh = [domain for domain in available if not is_throttled(domain)]
    source = fresh or available  # 全在冷却期时 fail-open，宁可撞限流也不停摆
    proven: list[str] = []
    unknown: list[str] = []
    risky: list[str] = []
    for domain in source:
        item = items.get(domain)
        if _proven(item):
            proven.append(domain)
        elif int((item or {}).get("consecutive_no_code") or 0) > 0:
            risky.append(domain)
        else:
            unknown.append(domain)
    if len(proven) >= _proven_pool_min():
        explore_now = random.random() < _explore_ratio() and len(unknown) >= _explore_min_pool()
        tier = "explore" if explore_now else "proven"
        candidates = unknown if explore_now else proven
    elif proven:
        # proven 池还小：在"已验证"和"未探索"之间均分，兼顾利用与发现。
        tier = "mixed"
        candidates = proven + unknown
    else:
        tier = "discover"
        candidates = unknown
    if not candidates:
        candidates = risky or source
        tier = "fallback"
    selected = random.choice(candidates)
    logger.info({
        "event": "mail_domain_selected",
        "provider": str(provider or ""),
        "domain": selected,
        "tier": tier,
        "available": len(available),
        "throttled": len(available) - len(fresh),
        "proven": len(proven),
        "unknown": len(unknown),
        "risky": len(risky),
    })
    return selected


def record_domain_result(provider: str, email_or_domain: object, outcome: str, error: object = "") -> dict[str, Any]:
    """回报某次注册对域名的结果。

    outcome:
    - accepted：注册成功（正向，清零各类连续失败计数）
    - rejected：unsupported_email 等确定性拒绝（硬信号，按阈值拉黑）
    - delivered：成功收到验证码（强正向，进入 proven 池）
    - no_code：等待验证码超时（弱负向，连续多次且从未收到过验证码才拉黑）
    - neutral：与域名无关的失败，不计数
    """
    domain = normalize_domain(email_or_domain)
    action = str(outcome or "").strip().lower()
    if not domain or action not in _OUTCOMES:
        return {"matched": False, "blocked": False}
    newly_blocked = False
    newly_silent_blocked = False
    with _lock:
        items = _load_unlocked()
        item = items.setdefault(domain, _default_item(domain))
        item["use_count"] = int(item.get("use_count") or 0) + 1
        item["last_used_at"] = _now_iso()
        if action == "delivered":
            item["code_received_count"] = int(item.get("code_received_count") or 0) + 1
            item["consecutive_no_code"] = 0
            item["last_code_at"] = _now_iso()
            item["last_error"] = ""
            item["silent_blocked_at"] = ""
        elif action == "no_code":
            item["no_code_count"] = int(item.get("no_code_count") or 0) + 1
            item["consecutive_no_code"] = int(item.get("consecutive_no_code") or 0) + 1
            item["last_no_code_at"] = _now_iso()
            item["last_error"] = str(error or "")[:200] or "verification code not delivered"
            if not item.get("silent_blocked_at") and _should_silent_block(item):
                item["silent_blocked_at"] = _now_iso()
                newly_silent_blocked = True
        elif action == "throttled":
            # 上游邮箱服务对该域名限流（不是域名本身不可用）：只做短冷却规避，
            # 不计入拒绝/丢信，避免把好域名误判成坏域名。
            item["throttled_at"] = _now_iso()
            item["throttle_count"] = int(item.get("throttle_count") or 0) + 1
        elif action == "accepted":
            item["success_count"] = int(item.get("success_count") or 0) + 1
            item["consecutive_rejections"] = 0
            item["consecutive_no_code"] = 0
            item["last_success_at"] = _now_iso()
            item["last_error"] = ""
            item["blocked_at"] = ""
            item["silent_blocked_at"] = ""
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
    if newly_silent_blocked:
        logger.warning({
            "event": "mail_domain_silent_blocked",
            "provider": str(provider or ""),
            "domain": domain,
            "consecutive_no_code": item["consecutive_no_code"],
            "code_received_count": int(item.get("code_received_count") or 0),
        })
    return {
        "matched": True,
        "domain": domain,
        "blocked": _is_blocked(item),
        "newly_blocked": newly_blocked,
        "newly_silent_blocked": newly_silent_blocked,
        "consecutive_rejections": int(item.get("consecutive_rejections") or 0),
        "consecutive_no_code": int(item.get("consecutive_no_code") or 0),
        "code_received_count": int(item.get("code_received_count") or 0),
    }


def stats_snapshot() -> dict[str, Any]:
    with _lock:
        items = _load_unlocked()
        if items and _unblock_expired_unlocked(items):
            _save_unlocked(items)
        entries = []
        for item in items.values():
            delivered = int(item.get("code_received_count") or 0)
            no_code = int(item.get("no_code_count") or 0)
            attempts = delivered + no_code
            entries.append({
                **item,
                "blocked": _is_blocked(item),
                "delivery_rate": round(delivered / attempts, 3) if attempts else None,
            })
        entries.sort(key=lambda entry: (
            -int(entry.get("total_rejections") or 0),
            -int(entry.get("no_code_count") or 0),
            entry["domain"],
        ))
        proven = [entry for entry in entries if int(entry.get("code_received_count") or 0) > 0]
        throttle_cooldown = _throttle_cooldown_seconds()
        now_ts = time.time()
        throttled = 0
        for entry in entries:
            raw = str(entry.get("throttled_at") or "")
            if not raw:
                continue
            try:
                if now_ts - datetime.fromisoformat(raw).timestamp() < throttle_cooldown:
                    throttled += 1
            except ValueError:
                continue
        return {
            "threshold": _threshold(),
            "cooldown_hours": round(_cooldown_seconds() / 3600.0, 2),
            "silent_threshold": _silent_threshold(),
            "silent_cooldown_hours": round(_silent_cooldown_seconds() / 3600.0, 2),
            "explore_ratio": _explore_ratio(),
            "throttle_cooldown_seconds": throttle_cooldown,
            "summary": {
                "total": len(entries),
                "blocked": sum(1 for entry in entries if entry["blocked"]),
                "silent_blocked": sum(1 for entry in entries if entry.get("silent_blocked_at")),
                "healthy": sum(
                    1
                    for entry in entries
                    if not entry["blocked"] and int(entry.get("success_count") or 0) > 0
                ),
                "delivery_proven": len(proven),
                "delivery_dead": sum(
                    1
                    for entry in entries
                    if int(entry.get("no_code_count") or 0) > 0 and int(entry.get("code_received_count") or 0) == 0
                ),
                "throttled": throttled,
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
        item["consecutive_no_code"] = 0
        item["blocked_at"] = ""
        item["silent_blocked_at"] = ""
        item["last_error"] = ""
        _save_unlocked(items)
    return {"ok": True, "domain": domain}
