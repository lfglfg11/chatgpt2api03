from __future__ import annotations

import asyncio
import faulthandler
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from utils.log import logger

_DUMP_DIR = Path(os.getenv("CHATGPT2API_FREEZE_DUMP_DIR", "") or "data/freeze-dumps")


def _env_float(name: str, default: float, minimum: float = 1.0) -> float:
    try:
        value = float(str(os.getenv(name, "") or default))
    except (TypeError, ValueError):
        value = default
    return max(value, minimum)


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(str(os.getenv(name, "") or default))
    except (TypeError, ValueError):
        value = default
    return max(value, minimum)


def _env_flag(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


class FreezeWatchdog:
    """事件循环心跳看门狗。

    背景：线上出现过「进程还活着、TCP 能连上、但一个请求都不回、日志完全停写」的假死，
    只能靠人工重启恢复。这里用一个独立线程给事件循环发心跳：连续 N 次在超时内拿不到
    回应就判定事件循环已被阻塞，先把所有线程栈 dump 到文件（定位真凶），再主动退出，
    让容器 restart 策略在数秒内拉起，避免几小时的停机。
    """

    def __init__(
            self,
            interval_seconds: float | None = None,
            failures: int | None = None,
            exit_on_freeze: bool | None = None,
            dump_dir: Path | None = None,
    ) -> None:
        self.interval = interval_seconds if interval_seconds is not None else _env_float(
            "CHATGPT2API_FREEZE_WATCHDOG_SECONDS", 15.0, 3.0)
        self.failures = failures if failures is not None else _env_int(
            "CHATGPT2API_FREEZE_WATCHDOG_FAILURES", 4)
        self.exit_on_freeze = exit_on_freeze if exit_on_freeze is not None else _env_flag(
            "CHATGPT2API_FREEZE_WATCHDOG_EXIT", True)
        self.dump_dir = dump_dir or _DUMP_DIR
        # 线程池饱和阈值：事件循环还活着、但所有 worker 线程都被卡住时，
        # 心跳仍然是通的（_ping 只是 sleep(0)），所以必须额外向线程池要 worker 来探活。
        # 线程数只作为二次确认，避免「瞬时高并发但池子还有余量」被误判成假死。
        self.thread_threshold = _env_int("CHATGPT2API_FREEZE_WATCHDOG_THREADS", 180, 20)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._consecutive_failures = 0
        self._consecutive_saturated = 0
        self._dumped = False
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- lifecycle -----------------------------------------------------
    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        try:
            faulthandler.enable()
        except Exception:
            pass
        self._loop = loop or asyncio.get_event_loop()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="freeze-watchdog", daemon=True)
        self._thread.start()
        logger.info({
            "event": "freeze_watchdog_started",
            "interval_seconds": self.interval,
            "failures": self.failures,
            "exit_on_freeze": self.exit_on_freeze,
            "dump_dir": str(self.dump_dir),
        })

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)

    # ---- internals -----------------------------------------------------
    @staticmethod
    async def _ping() -> None:
        await asyncio.sleep(0)

    @staticmethod
    async def _threadpool_ping() -> None:
        """向默认线程池要一个 worker：拿得到说明还有余量，拿不到说明池子已经饱和。

        不能直接读 `current_default_thread_limiter()`：它是 ContextVar，看门狗线程
        有自己的上下文，读到的会是一个新建的 40 令牌限流器（假象）。
        """
        import anyio.to_thread

        await anyio.to_thread.run_sync(lambda: None)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            try:
                future = asyncio.run_coroutine_threadsafe(self._ping(), loop)
                future.result(timeout=self.interval)
            except Exception as exc:  # TimeoutError / RuntimeError / loop closed
                self._consecutive_failures += 1
                logger.warning({
                    "event": "freeze_watchdog_heartbeat_missed",
                    "consecutive_failures": self._consecutive_failures,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                if self._consecutive_failures >= self.failures and not self._dumped:
                    self._handle_freeze("event_loop_blocked")
                continue
            self._consecutive_failures = 0

            # 心跳通不代表服务可用：所有 worker 线程被占满时，事件循环照样能跑 sleep(0)。
            try:
                probe = asyncio.run_coroutine_threadsafe(self._threadpool_ping(), loop)
                probe.result(timeout=self.interval)
            except Exception as exc:
                self._consecutive_saturated += 1
                threads = threading.active_count()
                logger.warning({
                    "event": "freeze_watchdog_threadpool_saturated",
                    "consecutive_failures": self._consecutive_saturated,
                    "threads": threads,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                if (
                    self._consecutive_saturated >= self.failures
                    and threads >= self.thread_threshold
                    and not self._dumped
                ):
                    self._handle_freeze("threadpool_saturated")
            else:
                self._consecutive_saturated = 0

    def _handle_freeze(self, reason: str = "event_loop_blocked") -> None:
        self._dumped = True
        blocked_seconds = round(self.interval * self.failures, 1)
        dump_path = ""
        try:
            self.dump_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = self.dump_dir / f"freeze-{stamp}.log"
            with open(target, "w", encoding="utf-8") as fp:
                fp.write(f"# {reason} for >= {blocked_seconds}s at {stamp}\n")
                fp.write(f"# pid={os.getpid()} threads={threading.active_count()}\n\n")
                faulthandler.dump_traceback(file=fp, all_threads=True)
            dump_path = str(target)
        except Exception as exc:
            dump_path = f"dump_failed: {type(exc).__name__}: {exc}"
        try:
            logger.error({
                "event": (
                    "freeze_watchdog_threadpool_exhausted"
                    if reason == "threadpool_saturated"
                    else "freeze_watchdog_event_loop_blocked"
                ),
                "reason": reason,
                "blocked_seconds": blocked_seconds,
                "dump_path": dump_path,
                "action": "exit" if self.exit_on_freeze else "log_only",
            })
        except Exception:
            pass
        if self.exit_on_freeze:
            # 直接退出，交给 docker restart policy 拉起；避免带着死锁的进程继续假装存活。
            os._exit(42)


freeze_watchdog = FreezeWatchdog()
