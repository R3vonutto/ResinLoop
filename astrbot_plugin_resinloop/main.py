"""ResinLoop · 游戏日常巡查（AstrBot 插件）。

原项目 ResinLoop（https://github.com/R3vonutto/ResinLoop ）是个 Windows 桌面程序：
常驻守护进程按时间点巡检米游社 / 森空岛的日常进度，没做完就通过 Bark 等通道提醒。
本插件把它的引擎（查询 / 判断 / 提醒）原样搬进 AstrBot：

    查询  米游社实时便笺（原神 / 崩铁 / 绝区零）+ 森空岛（明日方舟 / 终末地）
    判断  「每日委托 / 每日实训 / 每日活跃度」等判定项是否做完，顺带算体力溢出预警
    提醒  到点检查 → 主动发消息到订阅的群/私聊，也可继续用 Bark 等 Webhook

多人隔离（本版重点）：账号、历史存档、提醒订阅全部按「平台 + 发送者」分开存在
``users.json`` 里。群里 A 绑的号只有 A 能查到，B 随时可以绑自己的、互不覆盖；
定时检查也是一个一个人跑、各自发各自的，不会把某人的账号状态发到群里。

严格只读：不签到、不领奖励、不碰任何游戏数据。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

try:  # 正常情况：插件作为 data.plugins.<name> 子包加载
    from .rl import data_dir, login, pusher, runner, texts, userstore
    from .rl import settings as settings_mod
    from .rl import user_history
    from .rl.scheduler import CheckScheduler, next_run_at
    from .rl.settings import GAME_LABELS, PluginSettings
    from .rl.vendor.notifier import should_push
except ImportError:  # 少数加载方式下 main 是顶层模块，包上下文不存在
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from rl import data_dir, login, pusher, runner, texts, userstore
    from rl import settings as settings_mod
    from rl import user_history
    from rl.scheduler import CheckScheduler, next_run_at
    from rl.settings import GAME_LABELS, PluginSettings
    from rl.vendor.notifier import should_push

logger = logging.getLogger("astrbot_plugin_resinloop")

PLUGIN_NAME = "astrbot_plugin_resinloop"
KV_SLOT_STATE = "scheduler_slots"
KV_LAST_RUN = "last_run_at"

#: 命令别名（"/日常" 是主命令）
COMMAND_ALIASES = {"resinloop", "resin", "树脂", "日常巡查"}

#: 子命令关键词 → 规范动作
SUB_ACTIONS = {
    "": "check",
    "检查": "check",
    "now": "check",
    "状态": "status",
    "status": "status",
    "周报": "weekly",
    "weekly": "weekly",
    "活动": "activity",
    "activity": "activity",
    "订阅": "subscribe",
    "subscribe": "subscribe",
    "退订": "unsubscribe",
    "取消订阅": "unsubscribe",
    "unsubscribe": "unsubscribe",
    "解绑": "unbind",
    "删除绑定": "unbind",
    "unbind": "unbind",
    "认领": "claim",
    "claim": "claim",
    "登录米游社": "login_mihoyo",
    "米游社": "login_mihoyo",
    "mihoyo": "login_mihoyo",
    "登录森空岛": "login_skland",
    "森空岛": "login_skland",
    "skland": "login_skland",
    "测试推送": "test_push",
    "推送测试": "test_push",
    "帮助": "help",
    "help": "help",
    "?": "help",
}

#: 「解绑 xxx」里能认出来的游戏名（中文 / 英文标识都收）
GAME_ALIASES = {
    "原神": "genshin",
    "genshin": "genshin",
    "星铁": "starrail",
    "崩铁": "starrail",
    "崩坏星穹铁道": "starrail",
    "starrail": "starrail",
    "绝区零": "zzz",
    "zzz": "zzz",
    "明日方舟": "arknights",
    "方舟": "arknights",
    "arknights": "arknights",
    "终末地": "endfield",
    "endfield": "endfield",
}


def _command_tail(message: str) -> str:
    """从原始消息里抠出「子命令」部分。

    AstrBot 各版本对 message_str 是否带唤醒前缀/斜杠的处理不完全一致，
    这里不猜：切成词后，若首词是命令名（日常 / resin / 树脂 …）就丢掉它，
    剩下的整段当子命令；若首词本身就是子命令（命令名已被框架吃掉），原样返回。
    """
    tokens = (message or "").strip().split()
    if not tokens:
        return ""
    head = tokens[0].lstrip("/／").strip()
    if head == "日常" or head in COMMAND_ALIASES:
        return " ".join(tokens[1:]).strip()
    return " ".join(tokens).strip()


def _resolve_action(tail: str) -> Tuple[Optional[str], str]:
    """把子命令文本映射到动作，并返回动作后面的参数。"""
    key = (tail or "").strip()
    if not key:
        return SUB_ACTIONS[""], ""
    # 整段匹配（含「登录米游社」不带空格、「登录 米游社」被空格拆开两种）
    for candidate in (key, key.replace(" ", "")):
        action = SUB_ACTIONS.get(candidate)
        if action:
            return action, ""
    head = key.split()[0]
    action = SUB_ACTIONS.get(head)
    if action:
        return action, key[len(head):].strip()
    return None, ""


@register(
    PLUGIN_NAME,
    "R3vonutto / AstrBot 适配",
    "游戏日常巡查：米游社 / 森空岛日常未完成提醒（只查询，不签到；每人独立绑定）",
    "1.1.0",
    "https://github.com/R3vonutto/ResinLoop",
)
class ResinLoopPlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context, config)
        self.config: Any = config if config is not None else {}
        self._run_lock = asyncio.Lock()
        # 每用户一个扫码会话（多人同时扫各自的号，互不排队）
        self._login_sessions: Dict[str, str] = {}
        self._login_tasks: set[asyncio.Task] = set()
        # 查询缓存与冷却都按用户键，绝不跨人复用
        self._cache: Dict[str, Tuple[float, float, Any]] = {}
        self._cooldown: Dict[str, float] = {}
        self._store = userstore.UserStore()
        self._scheduler = CheckScheduler(
            get_settings=self._settings_async,
            on_slot=self._on_slot,
            get_state=self._get_slot_state,
            set_state=self._set_slot_state,
        )
        # 正常路径是 on_astrbot_loaded 拉起来；但插件若在运行中途被启用，
        # 那个钩子已经过去了，这里在事件循环存在的情况下先试一把。
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            self._ensure_scheduler()

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #

    def _settings(self) -> PluginSettings:
        """每次都重新解析配置：WebUI 改完不用重载插件。"""
        try:
            return settings_mod.load(self.config)
        except Exception:  # noqa: BLE001 - 配置写坏也要能回一句人话
            logger.exception("解析插件配置失败，回落到默认值")
            return PluginSettings()

    async def _settings_async(self) -> PluginSettings:
        return self._settings()

    async def _get_slot_state(self) -> Dict[str, Any]:
        try:
            state = await self.get_kv_data(KV_SLOT_STATE, {})
            return state if isinstance(state, dict) else {}
        except Exception:  # noqa: BLE001 - KV 不可用时退化成「今天没跑过」
            return {}

    async def _set_slot_state(self, state: Dict[str, Any]) -> None:
        try:
            await self.put_kv_data(KV_SLOT_STATE, state)
        except Exception as exc:  # noqa: BLE001
            logger.warning("记录检查状态失败（可能重复一轮检查）：%s", exc)

    async def _last_run(self) -> str:
        try:
            value = await self.get_kv_data(KV_LAST_RUN, "")
            return str(value or "")
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _now_stamp() -> str:
        from datetime import datetime

        return datetime.now().strftime("%m-%d %H:%M")

    async def _mark_run(self, key: str = "") -> str:
        stamp = self._now_stamp()
        try:
            await self.put_kv_data(KV_LAST_RUN, stamp)
        except Exception:  # noqa: BLE001
            pass
        if key:
            try:
                self._store.set_last_check(key, stamp)
            except Exception:  # noqa: BLE001 - 记不上不影响本轮
                logger.debug("写入个人检查时间失败", exc_info=True)
        return stamp

    def _ensure_scheduler(self) -> None:
        """按需拉起定时循环（插件在运行中被启用时也能生效）。"""
        if self._scheduler.running:
            return
        if not self._settings().enabled:
            logger.info("插件配置里 enabled=false，不启动定时检查")
            return
        self._scheduler.start()

    # ------------------------------------------------------------------ #
    # 用户身份
    # ------------------------------------------------------------------ #

    @staticmethod
    def _platform(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_platform_name() or "")
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _sender(event: AstrMessageEvent) -> str:
        try:
            value = str(event.get_sender_id() or "")
            if value:
                return value
        except Exception:  # noqa: BLE001
            pass
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        return umo.rsplit(":", 1)[-1] if ":" in umo else umo

    @staticmethod
    def _is_private(event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_private_chat())
        except Exception:  # noqa: BLE001
            return False

    def _user_key(self, event: AstrMessageEvent) -> str:
        return userstore.user_key(self._platform(event), self._sender(event))

    def _profile(self, event: AstrMessageEvent):
        """取（并顺手刷新）当前发送者的绑定资料。

        顺带做两件事：记住昵称用于日志；私聊过一次就记住私聊会话，
        定时提醒会优先发那里 —— 群里就不会出现别人的账号状态。
        """
        key = self._user_key(event)
        name = ""
        try:
            name = str(event.get_sender_name() or "")
        except Exception:  # noqa: BLE001
            pass
        profile = self._store.touch(
            key,
            name=name,
            platform=self._platform(event),
            sender_id=self._sender(event),
        )
        if self._is_private(event):
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if umo and profile.private_umo != umo:
                self._store.set_private_umo(key, umo)
                profile = self._store.profile(key)
        return profile

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        logger.info("ResinLoop 数据目录：%s", data_dir.ROOT_DIR)
        logger.info("ResinLoop 用户存储：%s", self._store.path)
        self._ensure_scheduler()

    async def terminate(self) -> None:
        """插件卸载/重载：停掉定时循环、取消还在等的扫码任务。"""
        await self._scheduler.stop()
        for task in list(self._login_tasks):
            task.cancel()
        self._login_tasks.clear()
        self._login_sessions.clear()
        logger.info("ResinLoop 插件已卸载")

    # ------------------------------------------------------------------ #
    # 主命令
    # ------------------------------------------------------------------ #

    @filter.command("日常", alias=COMMAND_ALIASES)
    async def daily(self, event: AstrMessageEvent):
        """游戏日常巡查：/日常 立即检查，/日常 状态 看概览，/日常 帮助 看全部指令。"""
        self._ensure_scheduler()
        action, arg = _resolve_action(_command_tail(event.message_str))
        if action is None:
            action = "help"

        if action == "help":
            yield event.plain_result(texts.help_text(self._settings()))
            return

        if action == "check":
            async for item in self._handle_check(event):
                yield item
            return

        if action == "status":
            yield event.plain_result(await self._status_message(event))
            return

        if action == "weekly":
            profile = self._profile(event)
            digest = await asyncio.to_thread(
                runner.weekly_digest, 7, user_history.path_for(profile.key)
            )
            body = texts.weekly_text(digest)
            if not profile.has_accounts:
                body = texts.binding_hint() + "\n\n" + body
            yield event.plain_result(body)
            return

        if action == "activity":
            profile = self._profile(event)
            text = await asyncio.to_thread(
                texts.activity_text, self._settings().for_accounts(profile.accounts)
            )
            yield event.plain_result(text)
            return

        if action == "subscribe":
            yield event.plain_result(self._toggle_subscription(event, True))
            return

        if action == "unsubscribe":
            yield event.plain_result(self._toggle_subscription(event, False))
            return

        if action == "unbind":
            yield event.plain_result(self._handle_unbind(event, arg))
            return

        if action == "claim":
            yield event.plain_result(self._handle_claim(event))
            return

        if action in ("login_mihoyo", "login_skland"):
            async for item in self._handle_login(event, action):
                yield item
            return

        if action == "test_push":
            async for item in self._handle_check(event, force_push=True):
                yield item
            return

    # ------------------------------------------------------------------ #
    # 检查
    # ------------------------------------------------------------------ #

    async def _handle_check(self, event: AstrMessageEvent, *, force_push: bool = False):
        settings = self._settings()
        profile = self._profile(event)
        if not profile.has_accounts:
            yield event.plain_result(texts.binding_hint())
            return

        key = profile.key
        now = time.monotonic()
        # 群里多人同时问时，标题要能看出这是谁的结果
        title = "" if self._is_private(event) else f"{profile.display} 的日常"

        if not force_push:
            wait = self._cooldown.get(key, 0.0) - now
            if wait > 0:
                yield event.plain_result(texts.cooldown_text(round(wait)))
                return

            cached = self._cache.get(key)
            if cached and cached[0] > now:
                age = int(now - cached[1])
                logger.info("%s 命中缓存（%d 秒前）", key, age)
                yield event.plain_result(
                    texts.report_text(cached[2].report, title=title) + texts.cache_note(age)
                )
                return

        yield event.plain_result("查询中…" if not force_push else "查询中，测试推送…")

        user_settings = settings.for_accounts(profile.accounts)
        result = await self._run_once(
            user_settings, history_path=user_history.path_for(key), dry_run=False
        )
        self._cooldown[key] = time.monotonic() + settings.command_cooldown_seconds
        if settings.query_cache_seconds > 0:
            self._cache[key] = (
                time.monotonic() + settings.query_cache_seconds,
                time.monotonic(),
                result,
            )
            self._prune_cache()

        await self._mark_run(key)
        yield event.plain_result(texts.report_text(result.report, title=title))

        if force_push:
            targets = self._resolve_targets(profile, settings)
            if not targets:
                yield event.plain_result(
                    "没有可推送的会话：先私聊我一次，或发 /日常 订阅。"
                )
                return
            push_body = texts.push_text(result.report)
            delivery = await pusher.push_to_sessions(
                self.context, targets, push_body
            )
            outcomes = pusher.DeliveryReport(delivery).outcomes
            ok = sum(1 for item in outcomes if item.ok)
            yield event.plain_result(
                f"推送 {ok}/{len(outcomes)} 成功" if outcomes else "没有可推送的会话"
            )
        elif result.cred_alerts:
            yield event.plain_result(texts.cred_alert_text(result.cred_alerts))

    def _prune_cache(self) -> None:
        """缓存表别无限长，顺手清掉过期的。"""
        if len(self._cache) < 200:
            return
        now = time.monotonic()
        expired = [key for key, item in self._cache.items() if item[0] <= now]
        for key in expired:
            self._cache.pop(key, None)
        for key, until in list(self._cooldown.items()):
            if until <= now:
                self._cooldown.pop(key, None)

    async def _run_once(self, settings: PluginSettings, *, dry_run: bool = False, history_path=None):
        """串行化执行：定时任务和手动检查撞一起时排队，不并发打接口。"""
        async with self._run_lock:
            return await runner.run_check(
                settings, dry_run=dry_run, history_path=history_path
            )

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #

    async def _status_message(self, event: AstrMessageEvent) -> str:
        settings = self._settings()
        profile = self._profile(event)
        scheduler_running = self._scheduler.running
        running = settings.for_accounts(profile.accounts)
        return texts.status_text(
            running,
            profile=profile,
            last_run=await self._last_run(),
            next_run=next_run_at(settings) if scheduler_running else None,
            scheduler_running=scheduler_running,
        )

    # ------------------------------------------------------------------ #
    # 定时检查：一个一个用户跑
    # ------------------------------------------------------------------ #

    async def _on_slot(self, slot: str, settings: PluginSettings) -> None:
        """到点执行：对每个有账号的用户各查一轮，各自决定推不推、推给谁。"""
        profiles = self._store.bindable_profiles()
        if not profiles:
            logger.info("定时检查 %s：还没有任何用户绑定账号，跳过", slot)
            return

        logger.info("定时检查 %s：共 %d 个用户要查", slot, len(profiles))
        webhook_bodies: List[str] = []
        single_report = None
        alert_count = 0
        stamp = self._now_stamp()

        for profile in profiles:
            targets = self._resolve_targets(profile, settings)
            try:
                user_settings = settings.for_accounts(profile.accounts)
                result = await self._run_once(
                    user_settings,
                    history_path=user_history.path_for(profile.key),
                    dry_run=False,
                )
            except Exception:  # noqa: BLE001 - 单个用户炸了不能拖垮整轮
                logger.exception("用户 %s 的定时检查失败", profile.key)
                continue

            if result.skipped_reason:
                logger.info("%s 跳过：%s", profile.key, result.skipped_reason)
                continue

            report = result.report
            alert, reason = should_push(
                settings.test_mode,
                report.need_alert,
                len(report.unfinished),
                always_notify=settings.always_notify,
                overflow_count=len(report.resource_alerts),
                activity_count=len(report.activity_alerts),
            )
            logger.info(
                "%s 推送决策：%s（%s）", profile.key, "推送" if alert else "不推送", reason
            )

            if len(profiles) == 1:
                single_report = report

            if alert:
                alert_count += 1
                if targets:
                    header = "" if len(profiles) == 1 else f"{profile.display} 的日常\n"
                    delivery = await pusher.push_to_sessions(
                        self.context, targets, f"{header}{texts.push_text(report)}"
                    )
                    logger.info("%s 推送结果：%s", profile.key, delivery)
                else:
                    logger.info("%s 有未完成项，但没有提醒去处（未私聊也未订阅）", profile.key)

            if result.cred_alerts and targets:
                delivery = await pusher.push_to_sessions(
                    self.context,
                    targets,
                    texts.cred_alert_text(result.cred_alerts),
                )
                logger.info("%s 凭证提醒：%s", profile.key, delivery)

            if summary := self._summary_line(profile, report):
                webhook_bodies.append(summary)

            self._store.set_last_check(profile.key, stamp)

        # 一个人都没触发提醒时不必打扰 Webhook（除非配置了「全部完成也通知」）
        if settings.webhook_enabled and alert_count:
            await self._deliver_webhook(settings, profiles, single_report, webhook_bodies)
        await self._mark_run()

    @staticmethod
    def _summary_line(profile, report) -> str:
        """给汇总通道用的一行摘要（只说数量，不暴露具体账号）。"""
        if not report.need_alert:
            return ""
        parts: List[str] = []
        if report.unfinished:
            parts.append(f"{len(report.unfinished)} 个游戏未完成")
        if report.resource_alerts:
            parts.append(f"{len(report.resource_alerts)} 个体力将溢出")
        if report.activity_alerts:
            parts.append(f"{len(report.activity_alerts)} 个活动将结束")
        return f"{profile.display}：{'、'.join(parts)}"

    async def _deliver_webhook(self, settings, profiles, single_report, summaries) -> None:
        """Webhook（Bark/飞书…）永远是「整台机器人一个配置」，这里做脱敏汇总。

        只有一个用户时直接发他的完整报告（单人使用体验不变）；
        多人时只发人数与计数，避免把某个人的账号状态发到别人的手机上。
        """
        try:
            if len(profiles) == 1 and single_report is not None:
                title = (
                    f"游戏日常 {len(single_report.finished)}/{len(single_report.statuses)}"
                )
                body = texts.push_text(single_report)
            else:
                if not summaries:
                    return
                title = f"{len(summaries)} 人有未完成项"
                body = "\n".join(summaries)
            delivery = await pusher.deliver(
                self.context, settings, title, body, targets=[]
            )
            logger.info("Webhook 汇总：%s", delivery.summary())
        except Exception:  # noqa: BLE001
            logger.exception("Webhook 汇总推送失败")

    # ------------------------------------------------------------------ #
    # 提醒去处（按用户）
    # ------------------------------------------------------------------ #

    def _resolve_targets(self, profile, settings: PluginSettings) -> List[str]:
        """按 push_scope 算出「这条提醒该发到哪」。

        private_first（默认）：私聊过就只发私聊（群里不会出现别人的账号状态），
        没私聊过才退回他订阅过的会话。
        """
        private = profile.private_umo
        subscribed = list(profile.targets)
        scope = settings.push_scope

        if scope == "private_only":
            return [private] if private else []
        if scope == "session_only":
            return subscribed
        # private_first
        if private:
            return [private]
        return subscribed

    # ------------------------------------------------------------------ #
    # 订阅 / 解绑 / 认领
    # ------------------------------------------------------------------ #

    def _toggle_subscription(self, event: AstrMessageEvent, subscribe: bool) -> str:
        profile = self._profile(event)
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return "拿不到当前会话标识，订阅失败（可能是平台适配器不支持）。"

        if subscribe:
            added = self._store.subscribe(profile.key, umo)
            profile = self._store.profile(profile.key)
            head = "已加入提醒列表" if added else "已经在提醒列表里"
            lines = [f"{head}（共 {len(profile.targets)} 个）：", umo]
            if not profile.has_accounts:
                lines.append("还没绑定账号：/日常 登录米游社 或 /日常 登录森空岛")
            return "\n".join(lines)

        removed = self._store.unsubscribe(profile.key, umo)
        if not removed:
            return "当前会话不在你的提醒列表里，无需退订。"
        profile = self._store.profile(profile.key)
        left = len(profile.targets)
        tail = f"还有 {left} 个会话。" if left else "已全部退订。"
        return f"已退订当前会话，{tail}"

    def _handle_unbind(self, event: AstrMessageEvent, arg: str) -> str:
        profile = self._profile(event)
        if not profile.accounts:
            return "你还没有绑定任何账号。"
        if not arg:
            bound = "、".join(
                GAME_LABELS.get(item.game, item.game) for item in profile.accounts
            )
            return (
                f"要解绑哪个游戏？你当前绑定：{bound}\n"
                "例：/日常 解绑 原神\n"
                "要全部删除请明确发：/日常 解绑 全部"
            )

        wanted = arg.strip().replace(" ", "")
        if wanted in ("全部", "所有", "all"):
            game = ""
        else:
            game = GAME_ALIASES.get(wanted) or (
                wanted if wanted in GAME_LABELS else ""
            )
            if not game:
                return (
                    f"不认识「{arg}」。可用的写法："
                    + "、".join(GAME_LABELS.values())
                    + "，或「全部」"
                )
        removed = self._store.unbind(profile.key, game)
        remain = len(self._store.profile(profile.key).accounts)
        logger.info("%s 解绑 %s：%s", profile.key, game or "全部", removed)
        return texts.unbind_text(removed, remain)

    def _handle_claim(self, event: AstrMessageEvent) -> str:
        """把旧版「全局配置账号」并到管理员名下，然后清空配置。"""
        if not event.is_admin():
            return "只有管理员可以把旧配置里的账号认领给某个人。"
        rows = self.config.get("accounts")
        if not isinstance(rows, list) or not rows:
            return texts.claim_text(0)

        profile = self._profile(event)
        settings = self._settings()
        added = self._store.claim_legacy(profile.key, rows, settings.push_targets)
        if added <= 0:
            return texts.claim_text(0)

        self.config["accounts"] = []
        self.config["push_targets"] = []
        self._save_config()
        if user_history.migrate_shared_to(profile.key):
            logger.info("旧的历史存档已复制给 %s", profile.key)
        return texts.claim_text(added)

    def _save_config(self) -> None:
        try:
            saver = getattr(self.config, "save_config", None)
            if callable(saver):
                saver()
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning("保存插件配置失败：%s", exc)

    # ------------------------------------------------------------------ #
    # 扫码登录（每人各自的会话）
    # ------------------------------------------------------------------ #

    async def _handle_login(self, event: AstrMessageEvent, action: str):
        profile = self._profile(event)
        key = profile.key

        if key in self._login_sessions:
            yield event.plain_result("已有扫码会话在等待，先完成或等 3 分钟超时。")
            return

        settings = self._settings()
        if len(self._login_sessions) >= settings.max_login_sessions:
            yield event.plain_result(
                f"扫码会话已满（{settings.max_login_sessions} 个），稍后再试。"
            )
            return

        kind = "mihoyo" if action == "login_mihoyo" else "skland"
        begin = login.begin_mihoyo if kind == "mihoyo" else login.begin_skland
        try:
            session = await begin(userstore.file_stem(key)[-10:])
        except login.LoginError as exc:
            yield event.plain_result(f"❌ {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("创建扫码会话失败")
            yield event.plain_result(f"❌ 创建扫码会话失败：{exc}")
            return

        chain: List[Any] = []
        if session.png is not None:
            chain.append(Image.fromFileSystem(str(session.png)))
        else:
            chain.append(Plain("二维码图片生成失败，链接：\n"))
            chain.append(Plain(session.url))
        tail = f"\n{session.hint()}，3 分钟内有效。"
        chain.append(Plain(tail))
        yield event.chain_result(chain)

        self._login_sessions[key] = kind
        task = asyncio.create_task(
            self._await_login(session, key, str(getattr(event, "unified_msg_origin", ""))),
            name=f"resinloop-login-{kind}-{key}",
        )
        self._login_tasks.add(task)
        task.add_done_callback(self._login_tasks.discard)

    async def _await_login(self, session: login.QrSession, key: str, umo: str) -> None:
        """后台等扫码结果，完成后写进这个用户自己的账号表。"""
        from astrbot.api.event import MessageChain

        try:
            if session.kind == "mihoyo":
                payload = await login.finish_mihoyo(session)
                rows = self._store.login_rows(key)
                games = settings_mod.apply_mihoyo_login(
                    {"accounts": rows}, payload["cookie"], payload.get("roles") or {}
                )
            else:
                payload = await login.finish_skland(session)
                rows = self._store.login_rows(key)
                games = settings_mod.apply_skland_login({"accounts": rows}, payload["token"])
            self._store.save_login_rows(key)
            text = texts.login_done_text(session.kind, games)
        except login.LoginError as exc:
            text = f"❌ 登录失败：{exc}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("扫码登录流程异常")
            text = f"❌ 登录失败：{exc}"
        finally:
            self._login_sessions.pop(key, None)

        try:
            await self.context.send_message(umo, MessageChain().message(text))
        except Exception as exc:  # noqa: BLE001 - 会话可能已经失效
            logger.warning("登录结果回传失败：%s", exc)

    # ------------------------------------------------------------------ #
    # LLM 工具：让 AI 也能查（同样是「谁问查谁」）
    # ------------------------------------------------------------------ #

    @filter.llm_tool(name="query_game_daily_status")
    async def query_game_daily_status(self, event: AstrMessageEvent, game: str = ""):
        """查询「这个人自己的」游戏日常任务完成情况：原神、崩坏：星穹铁道、绝区零、明日方舟、终末地。

        只读接口，不会签到、不会领奖励、不会修改任何游戏数据。
        当用户问「我今天的日常做完了吗」「树脂/体力满了没」「帮我看看还有哪些没做」时调用。

        Args:
            game(string): 可选，游戏名或标识（genshin/starrail/zzz/arknights/endfield）；留空表示查询本人已绑定的全部游戏。
        """
        profile = self._profile(event)
        if not profile.has_accounts:
            return "这位用户还没绑定账号，提示他发 /日常 登录米游社 或 /日常 登录森空岛。"

        settings = self._settings().for_accounts(profile.accounts)
        result = await self._run_once(
            settings, dry_run=True, history_path=user_history.path_for(profile.key)
        )
        report = result.report
        statuses = report.statuses

        wanted = (game or "").strip().lower()
        if wanted:
            alias = GAME_ALIASES.get(wanted, wanted)
            statuses = [item for item in statuses if item.game_id == alias]
            if not statuses:
                return f"没有查询到「{game}」的账号或数据。"

        lines: List[str] = []
        for item in statuses:
            lines.append(item.summary_line())
            for task in item.tasks:
                flag = "已完成" if task.done else "未完成"
                if not task.counted:
                    flag = "仅展示"
                progress = f"（{task.progress}）" if task.progress else ""
                lines.append(f"  - {task.name}{progress}：{flag}")
        for alert in report.resource_alerts:
            lines.append(alert.warn_line())
        if not lines:
            return "这一轮没有拿到任何有效数据，可能是凭证失效或网络异常。"
        return "\n".join(lines)
