#!/usr/bin/env python3
"""用历史注册日志回填邮箱域名"能否收到验证码"的信誉数据。

背景：GPTMail2 免费域名池里大部分域名会被静默丢信（OpenAI 不返回
unsupported_email，只是永远收不到验证码）。这类失败只体现在
"等待注册验证码超时"上，因此需要从日志回填，避免上线后重新摸索几小时。

用法（项目根目录）：
    PYTHONPATH=. python3 scripts/mail_domain_backfill.py --log /tmp/register.log
    PYTHONPATH=. python3 scripts/mail_domain_backfill.py --log /tmp/register.log --accounts /tmp/account_emails.txt
    PYTHONPATH=. python3 scripts/mail_domain_backfill.py --log /tmp/register.log --dry-run

⚠️ 必须在应用停止（或用一次性容器）时执行：应用进程每次回报域名结果都是
"读文件 → 改 → 整文件写回"，与外部写入并发时会用旧副本覆盖回填结果。

日志来源：`docker logs <容器> --since 24h 2>&1 | grep -E '任务[0-9]+' > /tmp/register.log`
账号列表来源：`GET /api/accounts` 的 items[].email（每行一个）。

回填规则（只增不减，幂等）：
- 收到验证码的任务 -> 对应域名 code_received_count 取观测值与现有值的较大者；
- 等待验证码超时的任务 -> no_code_count 取较大者；
- 有送达记录的域名不会被静默拉黑（consecutive_no_code 清零）。
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.register import mail_domain_stats as mds  # noqa: E402

MAILBOX_RE = re.compile(r"\[任务(\d+)\] 邮箱创建完成\[[^\]]+\]: [^@\s]+@([\w.-]+)")
TASK_RE = re.compile(r"\[任务(\d+)\]")
FAIL_RE = re.compile(r"任务(\d+)\s+注册失败")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
DELIVERED_MARKERS = ("收到注册验证码", "收到 Microsoft 登录验证码")
MISSING_MARKERS = ("等待注册验证码超时", "等待 Microsoft 登录验证码超时")


def scan(log_path: Path) -> tuple[dict[str, int], dict[str, int], int, int]:
    """返回 (送达计数, 丢信计数, 送达任务数, 丢信任务数)。"""
    delivered: dict[str, int] = defaultdict(int)
    missing: dict[str, int] = defaultdict(int)
    pending: dict[str, str] = {}
    ok_tasks = 0
    miss_tasks = 0
    with log_path.open(encoding="utf-8", errors="ignore") as handle:
        for raw in handle:
            line = ANSI_RE.sub("", raw)
            match = MAILBOX_RE.search(line)
            if match:
                domain = mds.normalize_domain(match.group(2))
                if domain:
                    pending[match.group(1)] = domain
                continue
            task = TASK_RE.search(line) or FAIL_RE.search(line)
            if not task:
                continue
            task_id = task.group(1)
            domain = pending.get(task_id)
            if not domain:
                continue
            if any(marker in line for marker in DELIVERED_MARKERS):
                delivered[domain] += 1
                ok_tasks += 1
                pending.pop(task_id, None)
            elif any(marker in line for marker in MISSING_MARKERS):
                missing[domain] += 1
                miss_tasks += 1
                pending.pop(task_id, None)
    return dict(delivered), dict(missing), ok_tasks, miss_tasks


def scan_accounts(accounts_path: Path) -> dict[str, int]:
    """统计账号池邮箱域名：能注册成功说明该域名确实送达过验证码。"""
    counts: dict[str, int] = defaultdict(int)
    for raw in accounts_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        text = raw.strip().strip('",')
        if not text:
            continue
        domain = mds.normalize_domain(text)
        if domain:
            counts[domain] += 1
    return dict(counts)


def main() -> int:
    parser = argparse.ArgumentParser(description="回填邮箱域名验证码送达信誉")
    parser.add_argument("--log", required=True, help="注册日志文件路径")
    parser.add_argument("--accounts", default="", help="账号邮箱列表文件（每行一个邮箱或域名），用于把已知能收信域名标为 proven")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写状态文件")
    parser.add_argument("--state", default="", help="覆盖信誉库路径（默认 data/register_domain_stats.json）")
    args = parser.parse_args()

    log_path = Path(args.log)
    if not log_path.is_file():
        print(f"日志不存在: {log_path}", file=sys.stderr)
        return 2
    if args.state:
        mds.STATE_PATH = Path(args.state)

    delivered, missing, ok_tasks, miss_tasks = scan(log_path)
    print(f"日志扫描完成：送达任务 {ok_tasks} 个，丢信任务 {miss_tasks} 个，涉及域名 {len(set(delivered) | set(missing))} 个")

    if args.accounts:
        accounts_path = Path(args.accounts)
        if not accounts_path.is_file():
            print(f"账号列表不存在: {accounts_path}", file=sys.stderr)
            return 2
        account_domains = scan_accounts(accounts_path)
        print(f"账号池覆盖 {len(account_domains)} 个域名（这些域名一定成功送达过验证码）")
        for domain, count in account_domains.items():
            delivered[domain] = max(delivered.get(domain, 0), count)

    with mds._lock:
        items = mds._load_unlocked()
        updated = 0
        for domain, count in delivered.items():
            item = items.setdefault(domain, mds._default_item(domain))
            if count > int(item.get("code_received_count") or 0):
                item["code_received_count"] = count
                item["last_code_at"] = item.get("last_code_at") or mds._now_iso()
                updated += 1
            item["consecutive_no_code"] = 0
            item["silent_blocked_at"] = ""
        for domain, count in missing.items():
            item = items.setdefault(domain, mds._default_item(domain))
            if count > int(item.get("no_code_count") or 0):
                item["no_code_count"] = count
                item["last_no_code_at"] = item.get("last_no_code_at") or mds._now_iso()
                updated += 1
            if int(item.get("code_received_count") or 0) == 0:
                item["consecutive_no_code"] = max(
                    int(item.get("consecutive_no_code") or 0), min(count, mds._silent_threshold())
                )
                if (
                    item["consecutive_no_code"] >= mds._silent_threshold()
                    and not item.get("silent_blocked_at")
                ):
                    item["silent_blocked_at"] = mds._now_iso()
        proven = sum(1 for item in items.values() if int(item.get("code_received_count") or 0) > 0)
        dead = sum(
            1
            for item in items.values()
            if int(item.get("no_code_count") or 0) > 0 and int(item.get("code_received_count") or 0) == 0
        )
        print(f"域名信誉更新条目 {updated} 个；proven(可送达)={proven}，静默丢信={dead}")
        if args.dry_run:
            print("--dry-run：未写入状态文件")
            return 0
        mds._save_unlocked(items)
    print(f"已写入 {mds.STATE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
