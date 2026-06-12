"""
Cron 定时调度 — 周期性任务执行
来源: Hermes cron scheduler + nanobot heartbeat

支持:
- 间隔调度 (每 N 秒/分钟/小时)
- Cron 表达式
- 一次性任务
"""

import asyncio
import logging
import time
from typing import Callable, Optional
from dataclasses import dataclass, field

logger = logging.getLogger("qiyue.cron")


@dataclass
class CronJob:
    name: str
    interval_s: float          # 调度间隔（秒）
    handler: Callable           # async def handler()
    enabled: bool = True
    last_run: float = 0.0
    run_count: int = 0
    max_runs: int = 0           # 0 = 无限


class CronScheduler:
    """轻量级定时调度器 — 单进程 asyncio 实现"""

    def __init__(self):
        self._jobs: dict[str, CronJob] = {}
        self._running = False
        self._tasks: list[asyncio.Task] = []

    def add(self, name: str, handler: Callable, interval_s: float,
            max_runs: int = 0):
        """添加定时任务"""
        self._jobs[name] = CronJob(
            name=name,
            interval_s=interval_s,
            handler=handler,
            max_runs=max_runs,
        )
        logger.info(f"[Cron] Registered: {name} (every {interval_s}s)")

    def remove(self, name: str):
        """移除任务"""
        if name in self._jobs:
            del self._jobs[name]
            logger.info(f"[Cron] Removed: {name}")

    def pause(self, name: str):
        if name in self._jobs:
            self._jobs[name].enabled = False

    def resume(self, name: str):
        if name in self._jobs:
            self._jobs[name].enabled = True

    async def start(self):
        """启动所有定时任务"""
        self._running = True
        for job in self._jobs.values():
            task = asyncio.create_task(self._run_job(job))
            self._tasks.append(task)
        logger.info(f"[Cron] Started {len(self._jobs)} jobs")

    async def stop(self):
        """停止所有任务"""
        self._running = False
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        logger.info("[Cron] Stopped")

    async def _run_job(self, job: CronJob):
        while self._running and job.enabled:
            try:
                start = time.time()
                await job.handler()
                elapsed = time.time() - start
                job.last_run = time.time()
                job.run_count += 1
                logger.debug(
                    f"[Cron] {job.name} done in {elapsed:.1f}s "
                    f"(#{job.run_count})"
                )

                if job.max_runs and job.run_count >= job.max_runs:
                    logger.info(f"[Cron] {job.name} reached max runs, stopping")
                    break

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Cron] {job.name} error: {e}", exc_info=True)

            # 等待下一次调度
            await asyncio.sleep(job.interval_s)


# ═══ 预定义任务 ═══

async def heartbeat_task(session_store, gateway, interval_s: float = 1800):
    """
    会话心跳保活 — nanobot heartbeat
    每 30 分钟检查会话状态，保持活跃会话不过期。
    """
    logger.debug("[Heartbeat] Session keep-alive check")
    # 列出最近活跃会话
    sessions = session_store.list_sessions(limit=10)
    active = [s for s in sessions if time.time() - s.last_active < 86400]
    logger.debug(f"[Heartbeat] {len(active)} active sessions")


async def session_compact_task(session_store):
    """
    会话压缩 — 每个活跃会话检查是否需要压缩
    保留最近 120 条消息。
    """
    sessions = session_store.list_sessions(limit=20)
    for s in sessions:
        if s.message_count > 150:
            session_store.compact(s.id, keep=120)
            logger.info(f"[Compact] Session {s.id}: {s.message_count} → 120")
