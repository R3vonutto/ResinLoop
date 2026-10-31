"""离线自测：不装 AstrBot 也能验证插件能加载、能跑通主链路。

做法：先塞一套最小的 astrbot 桩（只实现插件真正用到的那几个符号），
再 import main.py，用假的 AstrBotConfig / event 跑一遍命令与调度逻辑。

用法（插件目录下）：

    python tests/selftest.py

不联网、不写真实数据目录（写到 .selftest/），可以随时跑。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

# 每次从干净状态开始：凭证记账有 24 小时冷却，残留状态会让用例互相影响
_SELFTEST_DIR = PLUGIN_DIR / ".selftest"
shutil.rmtree(_SELFTEST_DIR, ignore_errors=True)
os.environ["RESINLOOP_DATA_DIR"] = str(_SELFTEST_DIR)


# --------------------------------------------------------------------------- #
# astrbot 桩
# --------------------------------------------------------------------------- #
def _install_astrbot_stub() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")

    # --- api.event ---
    event_mod = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:  # noqa: D401 - 桩
        pass

    class MessageChain:
        def __init__(self):
            self.text = ""

        def message(self, text: str) -> "MessageChain":
            self.text = text
            return self

    def _passthrough(*_args, **_kwargs):
        def decorator(func):
            return func

        return decorator

    filter_mod = types.ModuleType("astrbot.api.event.filter")
    for name in (
        "command",
        "command_group",
        "event_message_type",
        "platform_adapter_type",
        "permission_type",
        "regex",
        "custom_filter",
        "llm_tool",
        "on_astrbot_loaded",
        "on_platform_loaded",
        "on_llm_request",
        "on_llm_response",
        "on_decorating_result",
        "after_message_sent",
        "on_agent_begin",
        "on_agent_done",
        "on_using_llm_tool",
        "on_llm_tool_respond",
    ):
        setattr(filter_mod, name, _passthrough)

    class PermissionType:
        ADMIN = "admin"
        MEMBER = "member"

    filter_mod.PermissionType = PermissionType
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    event_mod.filter = filter_mod

    # --- api.message_components ---
    comp_mod = types.ModuleType("astrbot.api.message_components")

    class Plain:
        def __init__(self, text: str = ""):
            self.text = text

    class Image:
        def __init__(self, file: str = ""):
            self.file = file

        @staticmethod
        def fromFileSystem(path: str):
            return Image(file=str(path))

    comp_mod.Plain = Plain
    comp_mod.Image = Image

    # --- api.star ---
    star_mod = types.ModuleType("astrbot.api.star")

    class Context:
        def __init__(self):
            self.sent = []

        async def send_message(self, umo, chain):
            self.sent.append((umo, chain.text))

    class Star:
        plugin_id = "test/plugin"

        def __init__(self, context, config=None):
            self.context = context
            self.logger = types.SimpleNamespace(
                info=lambda *a, **k: None,
                warning=lambda *a, **k: None,
                error=lambda *a, **k: None,
                exception=lambda *a, **k: None,
                debug=lambda *a, **k: None,
            )
            self._kv = {}

        async def get_kv_data(self, key, default=None):
            return self._kv.get(key, default)

        async def put_kv_data(self, key, value):
            self._kv[key] = value

        async def delete_kv_data(self, key):
            self._kv.pop(key, None)

    def register(*_args, **_kwargs):
        def decorator(cls):
            return cls

        return decorator

    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.register = register

    astrbot.api = api
    api.event = event_mod
    api.message_components = comp_mod
    api.star = star_mod

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event_mod,
            "astrbot.api.event.filter": filter_mod,
            "astrbot.api.message_components": comp_mod,
            "astrbot.api.star": star_mod,
        }
    )


_install_astrbot_stub()

import main as plugin_main  # noqa: E402
from rl import runner  # noqa: E402
from rl import settings as settings_mod  # noqa: E402
from rl import texts, user_history  # noqa: E402
from rl.runner import collect_statuses  # noqa: E402
from rl.scheduler import CheckScheduler, next_run_at  # noqa: E402
from rl.vendor.analyzer import build_report  # noqa: E402
from rl.vendor.config import AccountConfig, AppConfig, QueryConfig  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"  {'✓' if condition else '✗'} {name}{'' if condition else '  ' + detail}")


# --------------------------------------------------------------------------- #
# 1. 配置解析
# --------------------------------------------------------------------------- #
SAMPLE_CONFIG = {
    "enabled": True,
    "check_times": ["08:30", "20:30", "25:99", "08:30"],
    "catch_up_minutes": 120,
    "push_targets": ["aiocqhttp:group:123456"],
    "always_notify": False,
    "test_mode": "auto",
    "resource_alert_enabled": True,
    "resource_thresholds": {"genshin": 180, "starrail": "0"},
    "activity_alert_enabled": True,
    "activity_alert_days": 5,
    "mihoyo_device_mode": "fp",
    "mihoyo_task_scope": "full",
    "accounts": [
        {
            "__template_key": "account",
            "game": "genshin",
            "nickname": "旅行者",
            "enabled": True,
            "cookie": "ltuid=123; ltoken=abc",
            "uid": "100000001",
            "server": "cn_gf01",
        },
        {"game": "arknights", "token": "tok", "enabled": "false"},
        {"game": "不存在的游戏"},
    ],
}


def test_settings() -> None:
    print("\n[1] 配置解析")
    cfg = settings_mod.load(SAMPLE_CONFIG)
    check("非法时间点被丢弃、重复去重", cfg.check_times == ["08:30", "20:30"], str(cfg.check_times))
    check("补发窗口按配置生效", cfg.catch_up_minutes == 120)
    check("字符串 false 被识别为关闭", cfg.accounts[1].enabled is False)
    check("未知游戏行被跳过", len(cfg.accounts) == 2, str(len(cfg.accounts)))
    check("阈值解析（含字符串 0）", cfg.thresholds["genshin"] == 180 and cfg.thresholds["starrail"] == 0)
    check("口径/设备模式透传", cfg.mihoyo_task_scope == "full" and cfg.mihoyo_device_mode == "fp")
    check("provider 有账号即真实数据源", cfg.provider == "miyoushe")

    empty = settings_mod.load({})
    check("空配置有默认时间点", empty.check_times == ["08:30", "20:30"] and not empty.ready)

    app = cfg.to_app_config()
    check("AppConfig 账号数一致", len(app.accounts) == 2)
    check("push.channels 为空（未开 webhook）", app.push.channels == [])

    hooks = settings_mod.load(
        {
            "webhook_enabled": True,
            "webhooks": {"bark_key": "bk", "qmsg_key": ""},
        }
    ).to_push_config()
    check("只构造填了值的通道", hooks.channels == ["bark"], str(hooks.channels))


def test_command_parsing() -> None:
    print("\n[2] 命令解析")
    cases = {
        "日常": "",
        "/日常": "",
        "日常 状态": "状态",
        "/日常 登录米游社": "登录米游社",
        "日常 登录 森空岛": "登录 森空岛",
        "resin 周报": "周报",
        "状态": "状态",
    }
    for message, expected in cases.items():
        got = plugin_main._command_tail(message)
        check(f"{message!r} → {expected!r}", got == expected, f"实际 {got!r}")

    actions = {
        "": ("check", ""),
        "状态": ("status", ""),
        "登录米游社": ("login_mihoyo", ""),
        "登录 森空岛": ("login_skland", ""),
        "测试推送": ("test_push", ""),
        "解绑 原神": ("unbind", "原神"),
        "不存在": (None, ""),
    }
    for tail, expected in actions.items():
        got = plugin_main._resolve_action(tail)
        check(f"动作 {tail!r} → {expected!r}", got == expected, f"实际 {got!r}")


# --------------------------------------------------------------------------- #
# 2. mock 数据源跑通查询 → 判断 → 文案
# --------------------------------------------------------------------------- #
def test_mock_pipeline() -> None:
    print("\n[3] mock 数据源全链路")
    app = AppConfig(
        accounts=[
            AccountConfig(game="genshin", nickname="旅行者"),
            AccountConfig(game="starrail", nickname="开拓者"),
            AccountConfig(game="zzz", nickname="绳匠"),
            AccountConfig(game="arknights", nickname="博士"),
            AccountConfig(game="endfield", nickname="管理员"),
        ],
        query=QueryConfig(provider="mock"),
    )
    statuses = collect_statuses(app)
    check("五款游戏都返回状态", len(statuses) == 5, str(len(statuses)))
    report = build_report(statuses, app.resource_thresholds)
    check("识别出未完成项", report.need_alert and len(report.unfinished) >= 1)
    check("报告文本非空", "每日任务检查结果" in report.to_console_text())
    title, body = report.to_push_payload()
    check("推送标题含进度", "游戏日常" in title, title)
    check("推送正文含未完成行", "[未完成]" in body)

    rendered = texts.report_text(report)
    check("聊天报告做了行数收敛", len(rendered.splitlines()) <= texts.MAX_REPORT_LINES + 2)

    # 体力预警：构造一个「树脂 180/200」的状态，阈值 160 应命中
    from rl.vendor.models import GameStatus, TaskItem

    resin_status = GameStatus(
        game_id="genshin",
        game_name="原神",
        tasks=[
            TaskItem("每日委托", True, "4/4"),
            TaskItem("原粹树脂", False, "180/200", counted=False),
        ],
        nickname="旅行者",
    )
    overflow = build_report([resin_status], {"genshin": 160})
    check("体力阈值命中", len(overflow.resource_alerts) == 1, str(overflow.resource_alerts))
    check("预警文案可读", "即将溢出" in overflow.resource_alerts[0].warn_line())
    check("预警会进推送标题", "体力将溢出" in overflow.to_push_payload()[0])


# --------------------------------------------------------------------------- #
# 3. 调度器
# --------------------------------------------------------------------------- #
def test_scheduler() -> None:
    print("\n[4] 定时与补发")
    cfg = settings_mod.load({"check_times": ["08:30"], "catch_up_minutes": 180, "accounts": SAMPLE_CONFIG["accounts"]})
    fired = []

    async def run() -> None:
        state = {}

        async def get_settings():
            return cfg

        async def on_slot(slot, _settings):
            fired.append(slot)

        async def get_state():
            return dict(state)

        async def set_state(value):
            state.update(value)

        scheduler = CheckScheduler(
            get_settings=get_settings,
            on_slot=on_slot,
            get_state=get_state,
            set_state=set_state,
        )

        day = datetime(2026, 10, 31, 0, 0, 0)
        await scheduler.tick(day.replace(hour=8, minute=0))
        check("到点前不触发", fired == [], str(fired))

        await scheduler.tick(day.replace(hour=8, minute=31))
        check("到点后触发一次", fired == ["08:30"], str(fired))

        await scheduler.tick(day.replace(hour=9, minute=30))
        check("同一天不重复触发", fired == ["08:30"], str(fired))

        await scheduler.tick(day + timedelta(days=1) + timedelta(hours=8, minutes=45))
        check("补发窗口内第二天照常触发", len(fired) == 2, str(fired))

        await scheduler.tick(day + timedelta(days=2) + timedelta(hours=14))
        check("超出补发窗口不触发", len(fired) == 2, str(fired))

    asyncio.run(run())
    nxt = next_run_at(cfg, datetime(2026, 10, 31, 23, 0))
    check("下次检查时间滚到明天", nxt == datetime(2026, 11, 1, 8, 30), str(nxt))


# --------------------------------------------------------------------------- #
# 4. 登录回写
# --------------------------------------------------------------------------- #
def test_login_writeback() -> None:
    print("\n[5] 扫码结果回写配置")
    cfg = {"accounts": [{"__template_key": "account", "game": "genshin", "cookie": "old", "uid": "", "server": ""}]}
    written = settings_mod.apply_mihoyo_login(
        cfg,
        "ltuid=1; ltoken=2",
        {
            "genshin": {"uid": "1001", "server": "cn_gf01", "nickname": "旅行者"},
            "starrail": {"uid": "2002", "server": "prod_gf_cn", "nickname": "开拓者"},
        },
    )
    rows = {row["game"]: row for row in cfg["accounts"]}
    check("原神行被更新", rows["genshin"]["cookie"] == "ltuid=1; ltoken=2" and rows["genshin"]["uid"] == "1001")
    check("崩铁行自动补建", "starrail" in rows and rows["starrail"]["uid"] == "2002")
    check("绝区零无角色不新增", "zzz" not in rows)
    check("回写清单可读", "原神" in written and "崩坏：星穹铁道" in written, str(written))

    cfg2 = {"accounts": []}
    written2 = settings_mod.apply_skland_login(cfg2, "tok", ["arknights"])
    check("森空岛行自动补建", cfg2["accounts"][0]["token"] == "tok" and written2 == ["明日方舟"])
    check("新行带 template_key", cfg2["accounts"][0]["__template_key"] == "account")


# --------------------------------------------------------------------------- #
# 5. 插件类：命令走通
# --------------------------------------------------------------------------- #
class FakeEvent:
    """最小事件桩：插件只用到这几个接口。"""

    def __init__(
        self,
        message: str,
        *,
        admin: bool = False,
        umo: str = "aiocqhttp:FriendMessage:1",
        sender: str = "1",
        platform: str = "aiocqhttp",
        name: str = "小明",
    ):
        self.message_str = message
        self.unified_msg_origin = umo
        self._admin = admin
        self._sender = sender
        self._platform = platform
        self._name = name

    def is_admin(self) -> bool:
        return self._admin

    def get_platform_name(self) -> str:
        return self._platform

    def get_sender_id(self) -> str:
        return self._sender

    def get_sender_name(self) -> str:
        return self._name

    def is_private_chat(self) -> bool:
        return "FriendMessage" in self.unified_msg_origin

    def plain_result(self, text: str):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)


def test_plugin_commands() -> None:
    print("\n[6] 插件命令")
    cfg = dict(SAMPLE_CONFIG)
    cfg["accounts"] = []  # 不绑定账号：避免真去请求米游社
    cfg["push_targets"] = []
    context = plugin_main.Context()
    plugin = plugin_main.ResinLoopPlugin(context, cfg)

    async def collect(event) -> list:
        return [item async for item in plugin.daily(event)]

    results = asyncio.run(collect(FakeEvent("日常")))
    check("/日常 无账号时给出绑定指引", "登录米游社" in results[-1][1], str(results)[:120])

    results = asyncio.run(collect(FakeEvent("日常 状态")))
    check("/日常 状态 有输出", "我的状态" in results[-1][1], results[-1][1][:60])

    results = asyncio.run(collect(FakeEvent("日常 帮助")))
    check("/日常 帮助 列出子命令", "登录森空岛" in results[-1][1] and "解绑" in results[-1][1])

    # 订阅：普通用户即可，写进自己的条目而不是插件配置
    group = FakeEvent("日常 订阅", umo="aiocqhttp:GroupMessage:555", sender="1")
    results = asyncio.run(collect(group))
    profile = plugin._store.profile("aiocqhttp:1")
    check("/日常 订阅 写进本人订阅", profile.targets == ["aiocqhttp:GroupMessage:555"], str(profile.targets))
    check("/日常 订阅 回执含会话数", "提醒列表" in results[-1][1], results[-1][1][:60])
    check("/日常 订阅 不再污染全局配置", plugin.config["push_targets"] == [])

    results = asyncio.run(collect(group))
    check("重复订阅提示已存在", "已经在你的提醒列表" in results[-1][1])

    results = asyncio.run(collect(FakeEvent("日常 退订", umo="aiocqhttp:GroupMessage:555", sender="1")))
    check("/日常 退订 清空本人订阅", plugin._store.profile("aiocqhttp:1").targets == [])
    check("/日常 退订 回执", "已退订当前会话" in results[-1][1], results[-1][1][:60])

    # 扫码登录：普通用户也能用（不再限管理员），且会话按人登记
    async def fake_begin(tag: str = ""):
        return types.SimpleNamespace(
            kind="mihoyo",
            url="https://example.invalid/qr",
            png=None,
            ticket="t",
            device_id="dev",
            scan_id="",
            hint=lambda: "请用米游社扫码",
        )

    async def fake_finish(session, timeout: int = 180):
        raise plugin_main.login.LoginError("离线自测不真的扫码")

    original_begin, original_finish = plugin_main.login.begin_mihoyo, plugin_main.login.finish_mihoyo
    plugin_main.login.begin_mihoyo = fake_begin
    plugin_main.login.finish_mihoyo = fake_finish
    try:

        async def login_flow():
            event = FakeEvent("日常 登录米游社", sender="9", name="乙", umo="aiocqhttp:FriendMessage:9")
            items = [item async for item in plugin.daily(event)]
            await asyncio.sleep(0.05)  # 让后台扫码任务（立即抛错）收尾
            return items

        results = asyncio.run(login_flow())
        check("登录不再要求管理员", results[-1][0] == "chain", str(results)[:80])
        check(
            "登录结果回传给发起者",
            any("登录失败" in text for umo, text in context.sent),
            str(context.sent)[:120],
        )
        check(
            "别的用户没被写入",
            plugin._store.profile("aiocqhttp:9").accounts == [],
            str(plugin._store.profile("aiocqhttp:9").rows),
        )
        check("扫码会话已释放", plugin._login_sessions == {}, str(plugin._login_sessions))
    finally:
        plugin_main.login.begin_mihoyo = original_begin
        plugin_main.login.finish_mihoyo = original_finish

    # 有账号但走 mock：直接验证 runner 的报告渲染路径
    cfg2 = dict(SAMPLE_CONFIG)
    plugin2 = plugin_main.ResinLoopPlugin(plugin_main.Context(), cfg2)
    settings2 = plugin2._settings()
    check("插件能解析出遗留账号", len(settings2.accounts) == 2)

    ok_sched = plugin2._settings().enabled
    check("插件未启动调度时 running=False", plugin2._scheduler.running is False)
    check("enabled 默认开启", ok_sched)


async def collect_daily(target_plugin, event) -> list:
    """把命令产出的消息收成列表（异步推导式必须待在协程里）。"""
    return [item async for item in target_plugin.daily(event)]


def test_multi_user() -> None:
    """两人各自绑定 → 各查各的、各推各的，互不覆盖。"""
    print("\n[7] 多人隔离")
    cfg = dict(SAMPLE_CONFIG)
    cfg["accounts"] = []
    cfg["push_targets"] = []
    cfg["always_notify"] = True   # 保证定时检查一定会推，便于验证「推给谁」
    cfg["query_cache_seconds"] = 90
    cfg["command_cooldown_seconds"] = 0
    context = plugin_main.Context()
    plugin = plugin_main.ResinLoopPlugin(context, cfg)

    # 用 mock 数据源：真数据源要联网，这里只验证「按人分流」
    original_provider = settings_mod.PluginSettings.provider
    settings_mod.PluginSettings.provider = property(lambda self: "mock")
    try:
        plugin._store.set_accounts(
            "aiocqhttp:1001",
            [{"game": "genshin", "nickname": "甲的原神", "enabled": True, "cookie": "ltuid=1"}],
        )
        plugin._store.set_accounts(
            "aiocqhttp:1002",
            [{"game": "zzz", "nickname": "乙的绝区零", "enabled": True, "cookie": "ltuid=2"}],
        )
        a = plugin._store.profile("aiocqhttp:1001")
        b = plugin._store.profile("aiocqhttp:1002")
        check("甲的账号只属于甲", [item.game for item in a.accounts] == ["genshin"])
        check("乙的账号只属于乙", [item.game for item in b.accounts] == ["zzz"])

        async def run(event) -> list:
            return [item async for item in plugin.daily(event)]

        ev_a = FakeEvent("日常", sender="1001", name="甲", umo="aiocqhttp:FriendMessage:1001")
        ev_b = FakeEvent("日常", sender="1002", name="乙", umo="aiocqhttp:FriendMessage:1002")
        ra = asyncio.run(run(ev_a))
        rb = asyncio.run(run(ev_b))
        text_a = "".join(item[1] for item in ra if item[0] == "plain")
        text_b = "".join(item[1] for item in rb if item[0] == "plain")
        check("甲只看到自己的原神", "原神" in text_a and "绝区零" not in text_a, text_a[:120])
        check("乙只看到自己的绝区零", "绝区零" in text_b and "原神" not in text_b, text_b[:120])
        check("两份结果不同", text_a != text_b)

        # 缓存只命中自己：紧接着再查一次，不该串到对方
        ra2 = asyncio.run(run(ev_a))
        check("甲的第二次查询命中自己的缓存", "缓存" in "".join(i[1] for i in ra2), str(ra2)[:120])

        # 历史存档一人一份
        path_a = user_history.path_for("aiocqhttp:1001")
        path_b = user_history.path_for("aiocqhttp:1002")
        check("甲的存档单独落盘", path_a.exists(), str(path_a))
        check("乙的存档单独落盘", path_b.exists(), str(path_b))
        check("两份存档不是同一个文件", path_a != path_b)

        digest_a = runner.weekly_digest(7, path_a)
        digest_b = runner.weekly_digest(7, path_b)
        games_a = {item.get("game") for item in digest_a.get("games", [])}
        games_b = {item.get("game") for item in digest_b.get("games", [])}
        check("甲的周报只有原神", games_a == {"genshin"}, str(games_a))
        check("乙的周报只有绝区零", games_b == {"zzz"}, str(games_b))

        # 订阅互不影响
        asyncio.run(run(FakeEvent("日常 订阅", sender="1001", umo="aiocqhttp:GroupMessage:777")))
        asyncio.run(run(FakeEvent("日常 订阅", sender="1002", umo="aiocqhttp:GroupMessage:888")))
        check(
            "订阅按人分开",
            plugin._store.profile("aiocqhttp:1001").targets == ["aiocqhttp:GroupMessage:777"]
            and plugin._store.profile("aiocqhttp:1002").targets == ["aiocqhttp:GroupMessage:888"],
        )

        # 定时检查：一人一条，且都发到各自私聊（群里不串号）
        context.sent.clear()
        asyncio.run(plugin._on_slot("08:30", plugin._settings()))
        sent_umos = [umo for umo, _ in context.sent]
        check("定时检查每个用户各推一条", len(context.sent) == 2, str(sent_umos))
        check(
            "推送落在各自私聊",
            sorted(sent_umos)
            == ["aiocqhttp:FriendMessage:1001", "aiocqhttp:FriendMessage:1002"],
            str(sent_umos),
        )
        bodies = {umo: text for umo, text in context.sent}
        check(
            "推送内容各不相同",
            "绝区零" not in bodies.get("aiocqhttp:FriendMessage:1001", "")
            and "原神" not in bodies.get("aiocqhttp:FriendMessage:1002", ""),
        )

        # 投递范围可配
        scope_settings = plugin._settings()
        profile_b = plugin._store.profile("aiocqhttp:1002")
        check(
            "默认私聊优先",
            plugin._resolve_targets(profile_b, scope_settings) == ["aiocqhttp:FriendMessage:1002"],
            str(plugin._resolve_targets(profile_b, scope_settings)),
        )
        scope_settings.push_scope = "session_only"
        check(
            "session_only 走订阅会话",
            plugin._resolve_targets(profile_b, scope_settings) == ["aiocqhttp:GroupMessage:888"],
            str(plugin._resolve_targets(profile_b, scope_settings)),
        )
        scope_settings.push_scope = "private_first"

        # 解绑必须指名，避免一句「解绑」把号全删
        results = asyncio.run(run(FakeEvent("日常 解绑", sender="1002", name="乙")))
        check("解绑不带参数只提示", "要解绑哪个游戏" in results[-1][1], results[-1][1][:60])
        check(
            "提示后账号没被删",
            [item.game for item in plugin._store.profile("aiocqhttp:1002").accounts] == ["zzz"],
        )

        # 解绑只影响自己
        removed = plugin._store.unbind("aiocqhttp:1001", "genshin")
        check("解绑只删自己的原神", removed == ["原神"], str(removed))
        check("甲已清空", plugin._store.profile("aiocqhttp:1001").accounts == [])
        check("乙毫发无损", [item.game for item in plugin._store.profile("aiocqhttp:1002").accounts] == ["zzz"])

        # 旧配置账号认领（连同旧历史存档一起迁移）
        from rl.vendor import history as history_mod

        legacy_file = history_mod.DATA_DIR / "history.json"
        legacy_file.parent.mkdir(parents=True, exist_ok=True)
        legacy_file.write_text(json.dumps({"version": 1, "days": {}}), encoding="utf-8")

        cfg_legacy = dict(SAMPLE_CONFIG)
        plugin_legacy = plugin_main.ResinLoopPlugin(plugin_main.Context(), cfg_legacy)
        claim_event = FakeEvent("日常 认领", admin=True, sender="2001", name="管理员")
        results = asyncio.run(collect_daily(plugin_legacy, claim_event))
        check("认领后配置被清空", plugin_legacy.config["accounts"] == [])
        claimed = plugin_legacy._store.profile("aiocqhttp:2001")
        check("旧账号已并入管理员", [item.game for item in claimed.accounts] == ["genshin", "arknights"])
        check("认领回执可读", "并入你名下" in results[-1][1], results[-1][1][:80])
        check(
            "旧历史存档随之迁移",
            user_history.path_for("aiocqhttp:2001").exists(),
            str(user_history.path_for("aiocqhttp:2001")),
        )

        # 普通用户不能认领别人的旧账号
        results = asyncio.run(
            collect_daily(plugin_legacy, FakeEvent("日常 认领", admin=False, sender="2002"))
        )
        check("非管理员不能认领", "管理员" in results[-1][1], results[-1][1][:60])
    finally:
        settings_mod.PluginSettings.provider = original_provider


def test_credentials_and_history() -> None:
    print("\n[8] 凭证解析 / 凭证记账 / 周报")
    from rl.vendor import history
    from rl.vendor.mihoyo_api import extract_credentials

    account = AccountConfig(
        game="genshin",
        nickname="旅行者",
        credential={"cookie": "ltuid=12345; ltoken=abcdef; cookie_token=zz", "uid": "100000001"},
    )
    ltuid, ltoken, uid, server, cookie = extract_credentials(account)
    check("从 Cookie 串里解析出 ltuid", ltuid == "12345", ltuid)
    check("区服走默认值", server == "cn_gf01", server)
    check("完整 Cookie 原样透传", cookie.endswith("cookie_token=zz"), cookie)

    bad = AccountConfig(game="genshin", credential={"cookie": "ltuid=1; ltoken=2", "uid": "100000000"})
    try:
        extract_credentials(bad)
        placeholder_ok = False
    except Exception as exc:  # 占位 UID 应报「未创建角色」而不是 TypeError
        placeholder_ok = "没有这款游戏的角色" in str(exc)
    check("占位 UID 被识别为未创建角色", placeholder_ok)

    # 凭证记账：连续两次 auth 失败且冷却期外 → 返回告警
    from rl.vendor.models import GameStatus

    app = AppConfig(accounts=[account], query=QueryConfig(provider="miyoushe"))
    failing = [
        GameStatus(game_id="genshin", game_name="原神", error="retcode=5003", cred_kind="auth")
    ]
    first = runner._record_cred_health(app, failing)
    second = runner._record_cred_health(app, failing)
    check("首次失败不告警", first == [])
    check("连续失败触发凭证告警", len(second) == 1 and second[0]["game_id"] == "genshin", str(second))

    # 历史 + 周报文案
    from rl.vendor.analyzer import build_report

    ok_status = GameStatus(
        game_id="genshin",
        game_name="原神",
        tasks=[TaskItemSimple("每日委托", True, "4/4")],
        nickname="旅行者",
    )
    history.record(build_report([ok_status]))
    digest = runner.weekly_digest(7)
    text = texts.weekly_text(digest)
    check("周报读到历史", digest.get("days_count", 0) >= 1, str(digest))
    check("周报文案含完成率", "%" in text, text[:60])

    status_text = texts.status_text(
        settings_mod.load(SAMPLE_CONFIG),
        last_run="10-31 08:30",
        next_run=datetime(2026, 11, 1, 8, 30),
        scheduler_running=True,
    )
    check("状态文案含账号与下次时间", "原神" in status_text and "11-01 08:30" in status_text)
    check("状态文案含体力预警口径", "体力预警：原神≥180" in status_text, status_text[-80:])
    check("活动文案能离线兜底", "活动" in texts.activity_text(settings_mod.load({})))


def TaskItemSimple(name: str, done: bool, progress: str = ""):
    from rl.vendor.models import TaskItem

    return TaskItem(name, done, progress)


def test_schema() -> None:
    print("\n[9] 元数据与配置 schema")
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    meta_keys = list(schema)
    for expected in ("enabled", "check_times", "push_targets", "accounts", "resource_thresholds", "webhooks"):
        check(f"schema 含 {expected}", expected in meta_keys)
    check("accounts 是 template_list", schema["accounts"]["type"] == "template_list")
    check(
        "账号模板字段齐全",
        set(schema["accounts"]["templates"]["account"]["items"])
        == {"game", "nickname", "enabled", "cookie", "uid", "server", "token"},
    )
    with open(PLUGIN_DIR / "metadata.yaml", encoding="utf-8") as handle:
        meta = handle.read()
    check("metadata 有 name/version/repo", all(k in meta for k in ("name:", "version:", "repo:")))
    check("i18n 文件可解析", json.loads((PLUGIN_DIR / ".astrbot-plugin/i18n/zh-CN.json").read_text(encoding="utf-8"))["metadata"]["display_name"] != "")


def main() -> int:
    test_settings()
    test_command_parsing()
    test_mock_pipeline()
    test_scheduler()
    test_login_writeback()
    test_plugin_commands()
    test_credentials_and_history()
    test_multi_user()
    test_schema()

    print("\n" + "=" * 52)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for name in FAIL:
        print(f"  失败：{name}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
