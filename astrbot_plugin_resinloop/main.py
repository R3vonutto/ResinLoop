"""ResinLoop · 游戏日常巡查（AstrBot 插件）。

原项目 ResinLoop（https://github.com/R3vonutto/ResinLoop ）是个 Windows 桌面程序：
常驻守护进程按时间点巡检米游社 / 森空岛的日常进度，没做完就通过 Bark 等通道提醒。
本插件把它的引擎（查询 / 判断 / 提醒）原样搬进 AstrBot：

    查询  米游社实时便笺（原神 / 崩铁 / 绝区零）+ 森空岛（明日方舟 / 终末地）
    判断  「每日委托 / 每日实训 / 每日活跃度」等判定项是否做完，顺带算体力溢出预警
    提醒  到点检查 → 主动发消息到订阅的群/私聊，也可继续用 Bark 等 Webhook

严格只读：不签到、不领奖励、不碰任何游戏数据。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

try:  # 正常情况：插件作为 data.plugins.<name> 子包加载
    from .rl import data_dir, login, pusher, runner, texts
    from .rl import settings as settings_mod
    from .rl.scheduler import CheckScheduler, next_run_at
    from .rl.settings import PluginSettings
    from .rl.vendor.notifier import should_push
except ImportError:  # 少数加载方式下 main 是顶层模块，包上下文不存在
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from rl import data_dir, login, pusher, runner, texts
    from rl import settings as settings_mod
    from rl.scheduler import CheckScheduler, next_run_at
    from rl.settings import PluginSettings
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


def _resolve_action(tail: str) -> Optional[str]:
    """把子命令文本映射到动作；支持「登录 米游社」这种被空格拆开的情况。"""
    key = (tail or "").strip()
    if not key:
        return SUB_ACTIONS[""]
    for candidate in (key, key.replace(" ", ""), key.split()[0]):
        action = SUB_ACTIONS.get(candidate)
        if action:
            return action
    return None


@register(
    PLUGIN_NAME,
    "R3vonutto / AstrBot 适配",
    "游戏日常巡查：米游社 / 森空岛日常未完成提醒（只查询，不签到）",
    "1.0.0",
    "https://github.com/R3vonutto/ResinLoop",
)
class ResinLoopPlugin(Star):
    def __init__(self, context: Context, config: Any = None):
        super().__init__(context, config)
        self.config: Any = config if config is not None else {}
        self._run_lock = asyncio.Lock()
        # 同一时刻只允许一个扫码会话在等（避免两条命令互相覆盖消息）
        self._login_kind: Optional[str] = None
        self._login_tasks: set[asyncio.Task] = set()
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

    async def _mark_run(self) -> str:
        from datetime import datetime

        stamp = datetime.now().strftime("%m-%d %H:%M")
        try:
            await self.put_kv_data(KV_LAST_RUN, stamp)
        except Exception:  # noqa: BLE001
            pass
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
    # 生命周期
    # ------------------------------------------------------------------ #

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        logger.info("ResinLoop 数据目录：%s", data_dir.ROOT_DIR)
        self._ensure_scheduler()

    async def terminate(self) -> None:
        """插件卸载/重载：停掉定时循环、取消还在等的扫码任务。"""
        await self._scheduler.stop()
        for task in list(self._login_tasks):
            task.cancel()
        self._login_tasks.clear()
        self._login_kind = None
        logger.info("ResinLoop 插件已卸载")

    # ------------------------------------------------------------------ #
    # 主命令
    # ------------------------------------------------------------------ #

    @filter.command("日常", alias=COMMAND_ALIASES)
    async def daily(self, event: AstrMessageEvent):
        """游戏日常巡查：/日常 立即检查，/日常 状态 看概览，/日常 帮助 看全部指令。"""
        self._ensure_scheduler()
        tail = _command_tail(event.message_str)
        action = _resolve_action(tail)
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
            settings = self._settings()
            yield event.plain_result(
                texts.status_text(
                    settings,
                    last_run=await self._last_run(),
                    next_run=next_run_at(settings),
                    scheduler_running=self._scheduler.running,
                )
            )
            return

        if action == "weekly":
            digest = await asyncio.to_thread(runner.weekly_digest, 7)
            yield event.plain_result(texts.weekly_text(digest))
            return

        if action == "activity":
            text = await asyncio.to_thread(texts.activity_text, self._settings())
            yield event.plain_result(text)
            return

        if action in ("subscribe", "unsubscribe"):
            if not event.is_admin():
                yield event.plain_result("只有管理员可以修改提醒订阅。")
                return
            _, message = self._toggle_subscription(
                event.unified_msg_origin, action == "subscribe"
            )
            yield event.plain_result(message)
            return

        if action in ("login_mihoyo", "login_skland"):
            async for item in self._handle_login(event, action):
                yield item
            return

        if action == "test_push":
            if not event.is_admin():
                yield event.plain_result("只有管理员可以测试推送。")
                return
            async for item in self._handle_check(event, force_push=True):
                yield item
            return

    # ------------------------------------------------------------------ #
    # 检查
    # ------------------------------------------------------------------ #

    async def _handle_check(self, event: AstrMessageEvent, *, force_push: bool = False):
        settings = self._settings()
        if not settings.ready:
            yield event.plain_result(
                "还没有绑定任何游戏账号。\n"
                "私聊机器人（管理员）发 /日常 登录米游社 或 /日常 登录森空岛 扫码即可。"
            )
            return

        yield event.plain_result("正在查询，稍等…" if not force_push else "正在查询并测试推送…")

        result = await self._run_once(settings, dry_run=False)
        await self._mark_run()
        yield event.plain_result(texts.report_text(result.report))

        if force_push:
            report = await self._deliver(settings, result.report)
            yield event.plain_result(f"推送结果：{report.summary()}")
        elif result.cred_alerts:
            yield event.plain_result(texts.cred_alert_text(result.cred_alerts))

    async def _run_once(self, settings: PluginSettings, *, dry_run: bool = False):
        """串行化执行：定时任务和手动检查撞一起时排队，不并发打接口。"""
        async with self._run_lock:
            return await runner.run_check(settings, dry_run=dry_run)

    async def _deliver(self, settings: PluginSettings, report) -> pusher.DeliveryReport:
        title, body = report.to_push_payload()
        return await pusher.deliver(self.context, settings, title, body)

    async def _on_slot(self, slot: str, settings: PluginSettings) -> None:
        """到点执行：查一轮 → 按规则决定要不要推。"""
        try:
            result = await self._run_once(settings, dry_run=False)
        except Exception:  # noqa: BLE001 - 单轮异常不能把调度循环带崩
            logger.exception("定时检查执行失败")
            return

        await self._mark_run()
        if result.skipped_reason:
            logger.info("定时检查跳过：%s", result.skipped_reason)
            return

        report = result.report
        alert, reason = should_push(
            settings.test_mode,
            report.need_alert,
            len(report.unfinished),
            always_notify=settings.always_notify,
            overflow_count=len(report.resource_alerts),
            activity_count=len(report.activity_alerts),
        )
        logger.info("定时检查 %s 推送决策：%s（%s）", slot, "推送" if alert else "不推送", reason)

        if alert and not settings.push_targets and not settings.webhook_enabled:
            logger.warning(
                "有未完成项，但还没有配置任何提醒接收方：在群里/私聊发 /日常 订阅 即可"
            )
            return

        if alert:
            delivery = await self._deliver(settings, report)
            logger.info("推送结果：%s", delivery.summary())

        if result.cred_alerts:
            delivery = await pusher.deliver(
                self.context,
                settings,
                "凭证可能失效",
                texts.cred_alert_text(result.cred_alerts),
            )
            logger.info("凭证提醒：%s", delivery.summary())

    # ------------------------------------------------------------------ #
    # 订阅
    # ------------------------------------------------------------------ #

    def _toggle_subscription(self, umo: str, subscribe: bool) -> tuple[bool, str]:
        targets = list(self._settings().push_targets)
        if subscribe:
            if umo in targets:
                return True, f"本会话已经在提醒列表里了：\n{umo}"
            targets.append(umo)
        else:
            if umo not in targets:
                return True, "本会话不在提醒列表里，无需退订。"
            targets = [item for item in targets if item != umo]
        self.config["push_targets"] = targets
        self._save_config()
        if subscribe:
            return True, f"已把本会话设为提醒接收方（共 {len(targets)} 个）：\n{umo}"
        return True, f"已退订，当前还有 {len(targets)} 个提醒会话。"

    def _save_config(self) -> None:
        try:
            saver = getattr(self.config, "save_config", None)
            if callable(saver):
                saver()
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning("保存插件配置失败：%s", exc)

    # ------------------------------------------------------------------ #
    # 扫码登录
    # ------------------------------------------------------------------ #

    async def _handle_login(self, event: AstrMessageEvent, action: str):
        if not event.is_admin():
            yield event.plain_result("扫码登录会写入账号凭证，只有管理员可以操作。")
            return
        if self._login_kind:
            yield event.plain_result(
                f"已经有一个 {self._login_kind} 扫码会话在等待，先完成或等它超时（3 分钟）。"
            )
            return

        kind = "mihoyo" if action == "login_mihoyo" else "skland"
        begin = login.begin_mihoyo if kind == "mihoyo" else login.begin_skland
        try:
            session = await begin()
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
            chain.append(Plain("（二维码图片生成失败，请把下面这串内容转成二维码）\n"))
            chain.append(Plain(session.url))
        chain.append(Plain(f"\n{session.hint()}，3 分钟内有效。"))
        yield event.chain_result(chain)

        self._login_kind = kind
        task = asyncio.create_task(
            self._await_login(session, event.unified_msg_origin),
            name=f"resinloop-login-{kind}",
        )
        self._login_tasks.add(task)
        task.add_done_callback(self._login_tasks.discard)

    async def _await_login(self, session: login.QrSession, umo: str) -> None:
        """后台等扫码结果，完成后回一条消息到发起命令的会话。"""
        from astrbot.api.event import MessageChain

        try:
            if session.kind == "mihoyo":
                payload = await login.finish_mihoyo(session)
                games = settings_mod.apply_mihoyo_login(
                    self.config, payload["cookie"], payload.get("roles") or {}
                )
                extra = f"设备已绑定：{payload['device_id'][:8]}…（三款游戏共用）"
            else:
                payload = await login.finish_skland(session)
                games = settings_mod.apply_skland_login(self.config, payload["token"])
                extra = f"Token 已保存（{settings_mod.mask_secret(payload['token'])}）"
            self._save_config()
            text = texts.login_done_text(session.kind, games, extra)
        except login.LoginError as exc:
            text = f"❌ 登录失败：{exc}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("扫码登录流程异常")
            text = f"❌ 登录失败：{exc}"
        finally:
            self._login_kind = None

        try:
            await self.context.send_message(umo, MessageChain().message(text))
        except Exception as exc:  # noqa: BLE001 - 会话可能已经失效
            logger.warning("登录结果回传失败：%s", exc)

    # ------------------------------------------------------------------ #
    # LLM 工具：让 AI 也能查日常
    # ------------------------------------------------------------------ #

    @filter.llm_tool(name="query_game_daily_status")
    async def query_game_daily_status(self, event: AstrMessageEvent, game: str = ""):
        """查询游戏「今天的日常任务」完成情况：原神、崩坏：星穹铁道、绝区零、明日方舟、终末地。

        只读接口，不会签到、不会领奖励、不会修改任何游戏数据。
        当用户问「我今天的日常做完了吗」「树脂/体力满了没」「帮我看看还有哪些没做」时调用。

        Args:
            game(string): 可选，游戏名或标识（genshin/starrail/zzz/arknights/endfield）；留空表示查询全部已绑定游戏。
        """
        settings = self._settings()
        if not settings.ready:
            return "用户还没有绑定任何游戏账号，无法查询。可提示其在管理员处执行 /日常 登录米游社 绑定。"

        result = await self._run_once(settings, dry_run=True)
        report = result.report
        statuses = report.statuses

        wanted = (game or "").strip().lower()
        if wanted:
            alias = {
                "原神": "genshin",
                "星铁": "starrail",
                "崩铁": "starrail",
                "崩坏星穹铁道": "starrail",
                "绝区零": "zzz",
                "明日方舟": "arknights",
                "方舟": "arknights",
                "终末地": "endfield",
            }.get(wanted, wanted)
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
