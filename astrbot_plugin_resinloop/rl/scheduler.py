"""定时检查。

上游用「常驻守护进程 + 系统托盘」，这里换成插件内的 asyncio 循环：
每 30 秒醒一次，看看有没有到点但今天还没跑过的检查时刻。

用 30 秒轮询而不是 cron 表达式，是为了三件事都能自然支持：
  * 多个时间点（08:30 / 20:30）；
  * 补发（电脑/机器人睡过头，醒过来还在补发窗口内就补跑一次，同上游行为）；
  * 改了配置立刻生效（每轮都重新读配置）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .settings import PluginSettings

logger = logging.getLogger("astrbot_plugin_resinloop.scheduler")

#: 轮询间隔（秒）
POLL_SECONDS = 30
#: 补发窗口为 0 时的容错分钟数：至少允许「到点后 2 分钟内」这一轮照常跑
MIN_TOLERANCE_MINUTES = 2


def _slot_dt(now: datetime, slot: str) -> datetime:
    hour, minute = (int(part) for part in slot.split(":"))
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def catch_up_window_minutes(settings: PluginSettings) -> int:
    return max(MIN_TOLERANCE_MINUTES, int(settings.catch_up_minutes))


def next_run_at(settings: PluginSettings, now: Optional[datetime] = None) -> Optional[datetime]:
    """下一次检查时间（用于状态展示）。今天全过点则顺延到明天的第一个时间点。"""
    current = now or datetime.now()
    today_slots: List[datetime] = []
    for slot in settings.check_times:
        try:
            moment = _slot_dt(current, slot)
        except (TypeError, ValueError):
            continue
        if moment > current:
            today_slots.append(moment)
    if today_slots:
        return min(today_slots)

    tomorrow = current + timedelta(days=1)
    for slot in sorted(settings.check_times):
        try:
            return _slot_dt(tomorrow, slot)
        except (TypeError, ValueError):
            continue
    return None


class CheckScheduler:
    """到点跑一轮检查的轮询器。

    Args:
        get_settings: 返回当前 PluginSettings 的协程（每轮都重新读配置）。
        on_slot: 真正执行检查的协程，参数为 (到点的时间点字符串, 配置快照)。
        get_state / set_state: 读写「今天已经跑过哪些时间点」的持久化钩子
            （用 AstrBot 的 KV 存储，插件重启后不重复跑、也不漏补发）。
    """

    def __init__(
        self,
        *,
        get_settings: Callable[[], Awaitable[PluginSettings]],
        on_slot: Callable[[str, PluginSettings], Awaitable[None]],
        get_state: Callable[[], Awaitable[Dict[str, Any]]],
        set_state: Callable[[Dict[str, Any]], Awaitable[None]],
        poll_seconds: int = POLL_SECONDS,
    ) -> None:
        self._get_settings = get_settings
        self._on_slot = on_slot
        self._get_state = get_state
        self._set_state = set_state
        self._poll_seconds = max(5, int(poll_seconds))
        self._task: Optional[asyncio.Task] = None
        self._running = False

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    def start(self) -> None:
        if self.running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="resinloop-scheduler")
        logger.info("定时检查已启动（轮询 %ss）", self._poll_seconds)

    async def stop(self) -> None:
        self._running = False
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        logger.info("定时检查已停止")

    async def _loop(self) -> None:
        # 启动先等几秒，让其它插件/平台先就绪
        await asyncio.sleep(5)
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 调度循环绝不能因为单轮异常退出
                logger.exception("定时检查轮次异常（已忽略，继续下一轮）")
            try:
                await asyncio.sleep(self._poll_seconds)
            except asyncio.CancelledError:
                raise

    async def tick(self, now: Optional[datetime] = None) -> Optional[str]:
        """检查一次是否该跑；返回本次触发的时间点（没触发返回 None）。"""
        settings = await self._get_settings()
        if not settings.enabled or not settings.ready:
            return None

        current = now or datetime.now()
        today = current.strftime("%Y-%m-%d")
        state = await self._get_state()
        if not isinstance(state, dict) or state.get("date") != today:
            state = {"date": today, "done": []}
        done: List[str] = [str(item) for item in state.get("done") or []]

        window = timedelta(minutes=catch_up_window_minutes(settings))
        due: Optional[str] = None
        changed = False
        for slot in settings.check_times:
            if slot in done:
                continue
            try:
                moment = _slot_dt(current, slot)
            except (TypeError, ValueError):
                done.append(slot)
                changed = True
                continue
            if moment > current:
                continue
            if current - moment > window:
                # 错过太久了：记成已跳过，不然每 30 秒都试一次
                done.append(slot)
                changed = True
                logger.info("跳过 %s 的检查（已超出补发窗口）", slot)
                continue
            due = slot
            break

        if due is None:
            if changed:
                await self._set_state({"date": today, "done": done})
            return None

        done.append(due)
        await self._set_state({"date": today, "done": done})
        logger.info("到点检查：%s", due)
        await self._on_slot(due, settings)
        return due


__all__ = ["CheckScheduler", "POLL_SECONDS", "catch_up_window_minutes", "next_run_at"]
