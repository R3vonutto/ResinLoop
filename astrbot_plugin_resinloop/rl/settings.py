"""插件配置 → 引擎配置。

AstrBot 的配置是「WebUI 表单 + dict」，引擎要的是强类型对象，转换全部集中在这里，
保证 main.py 只做编排、不碰脏活。所有字段都容错：填错不外抛，回落到默认值，
免得一个手滑的阈值让整个插件加载失败。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .vendor import config as engine_config
from .vendor.config import (
    AccountConfig,
    AppConfig,
    BarkConfig,
    DingTalkConfig,
    FeishuConfig,
    LoggingConfig,
    PushConfig,
    QueryConfig,
    QmsgConfig,
    ServerChanConfig,
)

#: 账号模板的 template_key（与 _conf_schema.json 的 templates 键一致）
ACCOUNT_TEMPLATE_KEY = "account"

#: 五款游戏：顺序即展示顺序
GAME_ORDER = ("genshin", "starrail", "zzz", "arknights", "endfield")
GAME_LABELS: Dict[str, str] = dict(engine_config.KNOWN_GAMES)

#: 米游社系三款（同一套扫码 Cookie）
MIHOYO_GAMES = ("genshin", "starrail", "zzz")
#: 森空岛系两款（同一个鹰角 token）
SKLAND_GAMES = ("arknights", "endfield")

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

DEFAULT_THRESHOLDS: Dict[str, int] = dict(engine_config.RESOURCE_THRESHOLD_DEFAULT)


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "开", "开启"):
        return True
    if text in ("0", "false", "no", "off", "关", "关闭"):
        return False
    return default


def _as_int(value: Any, default: int, *, low: int = 0, high: int = 10_000) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


def _as_list(value: Any) -> List[str]:
    """把 list / 逗号（换行、顿号）分隔的字符串统一成字符串列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value)
    for sep in ("\n", "，", "、", ";", "；", ","):
        text = text.replace(sep, " ")
    return [item.strip() for item in text.split(" ") if item.strip()]


def parse_check_times(value: Any) -> List[str]:
    """解析检查时间点：接受 list 或 "08:30,20:30"，非法项直接丢掉。"""
    result: List[str] = []
    for item in _as_list(value):
        match = _TIME_RE.match(item)
        if not match:
            continue
        text = f"{int(match.group(1)):02d}:{match.group(2)}"
        if text not in result:
            result.append(text)
    return sorted(result)


def parse_thresholds(value: Any) -> Dict[str, int]:
    """解析体力溢出阈值：留空/0 表示该游戏关闭预警。"""
    merged = dict(DEFAULT_THRESHOLDS)
    if isinstance(value, dict):
        for game in GAME_ORDER:
            if game in value:
                raw = value.get(game)
                if raw is None or (isinstance(raw, str) and not raw.strip()):
                    continue
                merged[game] = _as_int(raw, merged[game], low=0, high=9999)
    elif isinstance(value, str) and value.strip():
        # 兼容手写 "genshin=160,starrail=240"
        for part in value.replace("，", ",").split(","):
            key, _, raw = part.partition("=")
            key = key.strip().lower()
            if key in merged and raw.strip():
                merged[key] = _as_int(raw, merged[key], low=0, high=9999)
    return merged


def _parse_accounts(value: Any) -> List[AccountConfig]:
    """template_list → AccountConfig 列表（游戏名不认识的行直接跳过）。"""
    accounts: List[AccountConfig] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        game = str(item.get("game") or "").strip().lower()
        if game not in GAME_LABELS:
            continue
        credential: Dict[str, str] = {}
        for key in ("cookie", "token", "ltuid", "ltoken", "uid", "server"):
            raw = str(item.get(key) or "").strip()
            if raw:
                credential[key] = raw
        accounts.append(
            AccountConfig(
                game=game,
                nickname=str(item.get("nickname") or "").strip(),
                enabled=_as_bool(item.get("enabled", True)),
                credential=credential,
            )
        )
    return accounts


@dataclass
class PluginSettings:
    """一次快照的插件设置（每次用到都从 self.config 重新解析，改了配置立即生效）。"""

    enabled: bool = True
    check_times: List[str] = field(default_factory=lambda: ["08:30", "20:30"])
    catch_up_minutes: int = 180
    push_targets: List[str] = field(default_factory=list)
    always_notify: bool = False
    test_mode: str = "auto"

    resource_alert_enabled: bool = True
    thresholds: Dict[str, int] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    activity_alert_enabled: bool = True
    activity_alert_days: int = 3

    proxy: str = ""
    mihoyo_enabled: bool = True
    mihoyo_device_mode: str = "auto"
    mihoyo_task_scope: str = "daily"

    accounts: List[AccountConfig] = field(default_factory=list)
    webhooks: Dict[str, str] = field(default_factory=dict)
    webhook_enabled: bool = False

    @property
    def enabled_accounts(self) -> List[AccountConfig]:
        return [item for item in self.accounts if item.enabled]

    @property
    def game_ids(self) -> List[str]:
        """按 GAME_ORDER 去重后的已启用游戏（用于「支持哪些游戏」这类提示）。"""
        seen: List[str] = []
        for account in self.enabled_accounts:
            if account.game not in seen:
                seen.append(account.game)
        return seen

    @property
    def provider(self) -> str:
        """有账号就用真实数据源；一个都没绑就走 mock（引擎里 mock 只用于自检）。"""
        return "miyoushe" if self.enabled_accounts else "mock"

    @property
    def ready(self) -> bool:
        return bool(self.enabled_accounts)

    def thresholds_by_game(self) -> Dict[str, int]:
        if not self.resource_alert_enabled:
            return {game: 0 for game in GAME_ORDER}
        return dict(self.thresholds)

    def to_app_config(self) -> AppConfig:
        """转成引擎的 AppConfig，之后的查询/判断全部基于它。"""
        return AppConfig(
            test_mode=self.test_mode if self.test_mode in engine_config.VALID_TEST_MODES
            else engine_config.TEST_MODE_AUTO,
            check_times=list(self.check_times) or ["08:30"],
            catch_up_minutes=self.catch_up_minutes,
            catch_up_enabled=self.catch_up_minutes > 0,
            resource_alert_enabled=self.resource_alert_enabled,
            activity_alert_enabled=self.activity_alert_enabled,
            activity_alert_days=self.activity_alert_days,
            genshin_resin_threshold=self.thresholds.get("genshin", 0),
            starrail_stamina_threshold=self.thresholds.get("starrail", 0),
            zzz_energy_threshold=self.thresholds.get("zzz", 0),
            arknights_ap_threshold=self.thresholds.get("arknights", 0),
            endfield_sanity_threshold=self.thresholds.get("endfield", 0),
            accounts=list(self.accounts),
            push=self.to_push_config(),
            query=QueryConfig(
                provider=self.provider,
                timeout=10,
                mihoyo_enabled=self.mihoyo_enabled,
                proxy=self.proxy,
                mihoyo_device_mode=self.mihoyo_device_mode,
                mihoyo_task_scope=self.mihoyo_task_scope,
            ),
            logging=LoggingConfig(level="INFO"),
        )

    def to_push_config(self) -> PushConfig:
        """只有填了对应 Webhook 的通道才会真的推送（空值一律视为未配置）。"""
        hooks = self.webhooks
        bark = str(hooks.get("bark_key") or "").strip()
        serverchan = str(hooks.get("serverchan_key") or "").strip()
        qmsg = str(hooks.get("qmsg_key") or "").strip()
        feishu = str(hooks.get("feishu_webhook") or "").strip()
        dingtalk = str(hooks.get("dingtalk_webhook") or "").strip()

        channels = [
            name
            for name, value in (
                ("bark", bark),
                ("serverchan", serverchan),
                ("qmsg", qmsg),
                ("feishu", feishu),
                ("dingtalk", dingtalk),
            )
            if value
        ]
        return PushConfig(
            channels=channels if self.webhook_enabled else [],
            always_notify=self.always_notify,
            bark=BarkConfig(enabled=bool(bark), device_key=bark),
            serverchan=ServerChanConfig(enabled=bool(serverchan), sendkey=serverchan),
            qmsg=QmsgConfig(enabled=bool(qmsg), key=qmsg),
            feishu=FeishuConfig(
                enabled=bool(feishu),
                webhook=feishu,
                secret=str(hooks.get("feishu_secret") or "").strip(),
            ),
            dingtalk=DingTalkConfig(
                enabled=bool(dingtalk),
                webhook=dingtalk,
                secret=str(hooks.get("dingtalk_secret") or "").strip(),
            ),
        )


def load(config: Any) -> PluginSettings:
    """把 AstrBotConfig（或任意 dict）解析成 PluginSettings。"""
    raw: Dict[str, Any] = dict(config or {})

    test_mode = str(raw.get("test_mode") or "auto").strip().lower()
    if test_mode not in engine_config.VALID_TEST_MODES:
        test_mode = engine_config.TEST_MODE_AUTO

    device_mode = str(raw.get("mihoyo_device_mode") or "auto").strip().lower()
    if device_mode not in QueryConfig.DEVICE_MODES:
        device_mode = "auto"
    task_scope = str(raw.get("mihoyo_task_scope") or "daily").strip().lower()
    if task_scope not in QueryConfig.TASK_SCOPES:
        task_scope = "daily"

    webhooks_raw = raw.get("webhooks") if isinstance(raw.get("webhooks"), dict) else {}

    return PluginSettings(
        enabled=_as_bool(raw.get("enabled"), True),
        check_times=parse_check_times(raw.get("check_times")) or ["08:30", "20:30"],
        catch_up_minutes=_as_int(raw.get("catch_up_minutes"), 180, low=0, high=720),
        push_targets=[item for item in _as_list(raw.get("push_targets")) if ":" in item],
        always_notify=_as_bool(raw.get("always_notify"), False),
        test_mode=test_mode,
        resource_alert_enabled=_as_bool(raw.get("resource_alert_enabled"), True),
        thresholds=parse_thresholds(raw.get("resource_thresholds")),
        activity_alert_enabled=_as_bool(raw.get("activity_alert_enabled"), True),
        activity_alert_days=_as_int(raw.get("activity_alert_days"), 3, low=1, high=30),
        proxy=str(raw.get("proxy") or "").strip(),
        mihoyo_enabled=_as_bool(raw.get("mihoyo_enabled"), True),
        mihoyo_device_mode=device_mode,
        mihoyo_task_scope=task_scope,
        accounts=_parse_accounts(raw.get("accounts")),
        webhooks={str(k): "" if v is None else str(v) for k, v in webhooks_raw.items()},
        webhook_enabled=_as_bool(raw.get("webhook_enabled"), False),
    )


# --------------------------------------------------------------------------- #
# 登录回写：把扫码结果写进插件配置（accounts 是 template_list）
# --------------------------------------------------------------------------- #


def _account_rows(config: Any) -> List[Dict[str, Any]]:
    rows = config.get("accounts")
    if not isinstance(rows, list):
        rows = []
        config["accounts"] = rows
    return rows


def apply_mihoyo_login(config: Any, cookie: str, roles: Dict[str, Dict[str, Any]]) -> List[str]:
    """把扫码拿到的 Cookie 写进三款米游社游戏，并顺手回填 uid / server / 昵称。

    Args:
        config: 插件的 AstrBotConfig（会被原地修改，调用方负责 save_config）。
        cookie: 完整 Cookie 串。
        roles: ``fetch_game_roles`` 的结果 ``{game: {uid, server, nickname}}``。

    Returns:
        实际写入/更新的游戏中文名列表。
    """
    rows = _account_rows(config)
    touched: List[str] = []
    for game in MIHOYO_GAMES:
        role = roles.get(game) if isinstance(roles, dict) else None
        target = None
        for row in rows:
            if isinstance(row, dict) and str(row.get("game") or "").lower() == game:
                target = row
                break
        # 账号下没有这款游戏的角色：只在原本就勾了这款游戏时保留占位，不新增
        if role is None and target is None:
            continue
        if target is None:
            target = {"__template_key": ACCOUNT_TEMPLATE_KEY, "game": game, "enabled": True}
            rows.append(target)
        target["cookie"] = cookie
        for key in ("ltuid", "ltoken"):
            target.pop(key, None)
        target.setdefault("enabled", True)
        if role is not None:
            if role.get("uid"):
                target["uid"] = str(role["uid"])
            if role.get("server"):
                target["server"] = str(role["server"])
            if role.get("nickname"):
                target["nickname"] = str(role["nickname"])
        touched.append(GAME_LABELS.get(game, game))
    return touched


def apply_skland_login(config: Any, token: str, games: List[str] | None = None) -> List[str]:
    """把鹰角通行证 token 写进森空岛两款游戏（缺行则补一行）。"""
    rows = _account_rows(config)
    wanted = [game for game in (games or SKLAND_GAMES) if game in SKLAND_GAMES]
    touched: List[str] = []
    for game in wanted:
        target = None
        for row in rows:
            if isinstance(row, dict) and str(row.get("game") or "").lower() == game:
                target = row
                break
        if target is None:
            target = {"__template_key": ACCOUNT_TEMPLATE_KEY, "game": game, "enabled": True}
            rows.append(target)
        target["token"] = token
        target.setdefault("enabled", True)
        touched.append(GAME_LABELS.get(game, game))
    return touched


def mask_secret(value: str, keep: int = 4) -> str:
    """日志/回复里给凭证打码。"""
    text = (value or "").strip()
    if not text:
        return "(空)"
    if len(text) <= keep * 2:
        return text[:keep] + "***"
    return f"{text[:keep]}***{text[-keep:]}"


__all__ = [
    "ACCOUNT_TEMPLATE_KEY",
    "GAME_LABELS",
    "GAME_ORDER",
    "MIHOYO_GAMES",
    "SKLAND_GAMES",
    "PluginSettings",
    "apply_mihoyo_login",
    "apply_skland_login",
    "load",
    "mask_secret",
    "parse_check_times",
    "parse_thresholds",
]
