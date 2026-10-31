"""推送层：把一份报告发到 AstrBot 会话（群/私聊）以及可选的 Webhook。

与上游的区别：上游只有 Bark / Server酱 / Qmsg / 飞书 / 钉钉，
这里主通道换成「机器人自己发消息」，Webhook 作为可选补充保留。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, List

from .settings import PluginSettings
from .vendor import push_guard
from .vendor.notifier import build_notifiers, push_all

logger = logging.getLogger("astrbot_plugin_resinloop.pusher")


@dataclass
class PushOutcome:
    channel: str
    ok: bool
    message: str = ""

    def __str__(self) -> str:
        return f"[{self.channel}] {'成功' if self.ok else '失败'}{'：' + self.message if self.message else ''}"


@dataclass
class DeliveryReport:
    outcomes: List[PushOutcome] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for item in self.outcomes if item.ok)

    @property
    def failed(self) -> List[PushOutcome]:
        return [item for item in self.outcomes if not item.ok]

    def summary(self) -> str:
        if not self.outcomes:
            return "没有可用的推送通道"
        return "、".join(
            f"{item.channel}{'✓' if item.ok else '✗'}" for item in self.outcomes
        )


async def push_to_sessions(
    context: Any, targets: List[str], text: str
) -> List[PushOutcome]:
    """把文本发到每个配置的会话（UMO）。单个会话失败不影响其它会话。"""
    from astrbot.api.event import MessageChain

    outcomes: List[PushOutcome] = []
    for target in targets:
        try:
            await context.send_message(target, MessageChain().message(text))
            outcomes.append(PushOutcome(channel=target, ok=True))
        except Exception as exc:  # noqa: BLE001 - 单个会话异常（平台离线等）不中断
            logger.warning("推送到 %s 失败：%s", target, exc)
            outcomes.append(PushOutcome(channel=target, ok=False, message=str(exc)))
    return outcomes


def _push_webhooks_sync(settings: PluginSettings, title: str, body: str) -> List[PushOutcome]:
    """Webhook 推送（阻塞），带通道回压：连续失败的通道静默 6 小时。"""
    push_config = settings.to_push_config()
    notifiers = build_notifiers(push_config)
    if not notifiers:
        return []
    active = [item for item in notifiers if not push_guard.is_muted(item.name)]
    if len(active) != len(notifiers):
        logger.warning("推送通道回压：%d/%d 可用", len(active), len(notifiers))
    if not active:
        return []

    results = push_all(active, title, body)
    outcomes: List[PushOutcome] = []
    for result in results:
        outcomes.append(
            PushOutcome(channel=result.channel, ok=result.ok, message=result.message)
        )
        if push_guard.record(result.channel, result.ok):
            logger.warning("[%s] 连续失败，已静默 6 小时", result.channel)
    return outcomes


async def deliver(
    context: Any,
    settings: PluginSettings,
    title: str,
    body: str,
    *,
    targets: List[str] | None = None,
) -> DeliveryReport:
    """按「会话 → Webhook」的顺序发送，返回每个通道的结果。"""
    report = DeliveryReport()
    text = f"{title}\n{body}".strip()
    session_targets = targets if targets is not None else settings.push_targets

    if session_targets:
        report.outcomes.extend(await push_to_sessions(context, session_targets, text))

    if settings.webhook_enabled:
        try:
            hooks = await asyncio.to_thread(_push_webhooks_sync, settings, title, body)
            report.outcomes.extend(hooks)
        except Exception as exc:  # noqa: BLE001 - Webhook 炸了不能影响会话推送
            logger.warning("Webhook 推送异常：%s", exc)
            report.outcomes.append(
                PushOutcome(channel="webhook", ok=False, message=str(exc))
            )
    return report


__all__ = ["DeliveryReport", "PushOutcome", "deliver", "push_to_sessions"]
