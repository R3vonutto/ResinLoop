"""插件配置 → 引擎配置。

AstrBot 的配置是「WebUI 表单 + dict」，引擎要的是强类型对象，转换全部集中在这里，
保证 main.py 只做编排、不碰脏活。所有字段都容错：填错不外抛，回落到默认值，
免得一个手滑的阈值让整个插件加载失败。
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional

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

#: 定时提醒的投递范围（多人同群时决定「发给谁」）
PUSH_SCOPES = ("private_first", "session_only", "private_only")


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
    """template_list → AccountConfig 列表（游戏名不认识的行直接跳过）。

    同一个人可以有多行同一款游戏（两个手机号各一个号），这里的顺序就是
    存储顺序，查询/展示都按这个顺序走，不会互相覆盖。

    展示名优先取用户起的别名 ``label``：两个号都叫「博士」时，
    只有别名能把「大号 / 小号」区分开。
    """
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
                nickname=str(item.get("label") or item.get("nickname") or "").strip(),
                enabled=_as_bool(item.get("enabled", True)),
                credential=credential,
            )
        )
    return accounts


#: 公开别名：userstore 用同一套解析，保证「存下来的行」与「配置里的行」口径一致
parse_accounts = _parse_accounts


@dataclass
class PluginSettings:
    """一次快照的插件设置（每次用到都从 self.config 重新解析，改了配置立即生效）。

    ``accounts`` 在这里只表示「这一轮查谁」：多用户模式下由 :meth:`for_accounts`
    注入某个人的账号；插件配置里那份降级成遗留数据（用 ``/日常 认领`` 迁移）。
    """

    enabled: bool = True
    check_times: List[str] = field(default_factory=lambda: ["08:30", "20:30"])
    catch_up_minutes: int = 180
    push_targets: List[str] = field(default_factory=list)
    always_notify: bool = False
    test_mode: str = "auto"

    #: 定时提醒发到哪：private_first（私聊优先，群内不串号）/ session_only / private_only
    push_scope: str = "private_first"
    #: 同一个人重复查询的缓存秒数（0 关闭）：群里连着问几次不会重复打接口
    query_cache_seconds: int = 90
    #: 同一个人两次 /日常 之间的最小间隔（0 关闭）
    command_cooldown_seconds: int = 10
    #: 同时进行的扫码会话上限（多人各自扫码时互不阻塞）
    max_login_sessions: int = 3

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

    def for_accounts(self, accounts: List[AccountConfig]) -> "PluginSettings":
        """派生出「只看这个人的账号」的配置快照。"""
        return replace(self, accounts=list(accounts or []))

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

    push_scope = str(raw.get("push_scope") or "private_first").strip().lower()
    if push_scope not in PUSH_SCOPES:
        push_scope = "private_first"

    return PluginSettings(
        enabled=_as_bool(raw.get("enabled"), True),
        check_times=parse_check_times(raw.get("check_times")) or ["08:30", "20:30"],
        catch_up_minutes=_as_int(raw.get("catch_up_minutes"), 180, low=0, high=720),
        push_targets=[item for item in _as_list(raw.get("push_targets")) if ":" in item],
        always_notify=_as_bool(raw.get("always_notify"), False),
        test_mode=test_mode,
        push_scope=push_scope,
        query_cache_seconds=_as_int(raw.get("query_cache_seconds"), 90, low=0, high=3600),
        command_cooldown_seconds=_as_int(
            raw.get("command_cooldown_seconds"), 10, low=0, high=600
        ),
        max_login_sessions=_as_int(raw.get("max_login_sessions"), 3, low=1, high=10),
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
# 登录回写：把扫码结果合并进某个人的账号行
#
# 一个人可以有多行同一款游戏（明日方舟和终末地不在同一个手机号上是最常见的
# 例子）。所以回写不能「按游戏找到那行就覆盖」，必须按**账号身份**判断：
#
#   同一个 uid  → 是老号重新登录，就地刷新凭证（Cookie / token 会过期）
#   新 uid      → 是这个人的第二个号，追加一行
#
# 身份（uid）由登录阶段顺带拉取：米游社取 ``fetch_game_roles`` 的 uid，
# 森空岛取 binding 里的 uid（终末地退化成角色 roleId）。
# --------------------------------------------------------------------------- #


def new_row_id() -> str:
    """账号行的稳定 id（8 位十六进制）。用于跨重载认出「还是这一行」。"""
    return secrets.token_hex(4)


@dataclass
class LoginWriteback:
    """一次扫码写入了什么（给用户回执用）。"""

    added: List[Dict[str, str]] = field(default_factory=list)
    updated: List[Dict[str, str]] = field(default_factory=list)
    missing: List[Dict[str, str]] = field(default_factory=list)

    @property
    def touched(self) -> List[Dict[str, str]]:
        return self.added + self.updated

    @property
    def empty(self) -> bool:
        return not (self.added or self.updated)


def _account_rows(config: Any) -> List[Dict[str, Any]]:
    rows = config.get("accounts")
    if not isinstance(rows, list):
        rows = []
        config["accounts"] = rows
    return rows


def _entry(game: str, row: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    data = row or {}
    return {
        "game": game,
        "label": GAME_LABELS.get(game, game),
        "name": str(data.get("label") or data.get("nickname") or ""),
    }


def _blank_row(game: str, nickname: str = "") -> Dict[str, Any]:
    return {
        "__template_key": ACCOUNT_TEMPLATE_KEY,
        "id": new_row_id(),
        "game": game,
        "label": "",
        "nickname": nickname,
        "enabled": True,
    }


def _rows_of_game(rows: List[Dict[str, Any]], game: str) -> List[Dict[str, Any]]:
    return [
        row
        for row in rows
        if isinstance(row, dict) and str(row.get("game") or "").strip().lower() == game
    ]


def find_account_row(
    rows: List[Dict[str, Any]], game: str, uid: str = ""
) -> Optional[Dict[str, Any]]:
    """在账号行里找「同一个号」的那一行。

    * 给了 uid：先按 uid 精确匹配；匹配不到且这款游戏只有一行、且那行还没记
      uid（老版本写下的占位行），就认领它 —— 这样升级上来的用户不会平白多一行。
    * 没给 uid（接口没返回身份）：只有这款游戏正好一行时才复用它，
      多行时宁可新增也不猜，免得把别人的号覆盖掉。
    """
    candidates = _rows_of_game(rows, game)
    if not candidates:
        return None
    uid = (uid or "").strip()
    if uid:
        for row in candidates:
            if str(row.get("uid") or "").strip() == uid:
                return row
        if len(candidates) == 1 and not str(candidates[0].get("uid") or "").strip():
            return candidates[0]
        return None
    return candidates[0] if len(candidates) == 1 else None


def apply_mihoyo_login(
    config: Any,
    cookie: str,
    roles: Dict[str, Dict[str, Any]],
    games: Optional[List[str]] = None,
) -> LoginWriteback:
    """把扫码拿到的 Cookie 合并进米游社三款游戏的账号行。

    Args:
        config: ``{"accounts": rows}``（会被原地修改，调用方负责落盘）。
        cookie: 完整 Cookie 串。
        roles: ``fetch_game_roles`` 的结果 ``{game: {uid, server, nickname}}``。
        games: 这次只登哪几款；``None`` 表示三款都试（没角色的自动跳过）。

    Returns:
        :class:`LoginWriteback`：新增 / 刷新 / 该账号下没有的角色。
    """
    rows = _account_rows(config)
    wanted = [game for game in MIHOYO_GAMES if games is None or game in games]
    result = LoginWriteback()
    for game in wanted:
        role = roles.get(game) if isinstance(roles, dict) else None
        if not role:
            # 这个手机号没玩这款游戏：不建占位行，也绝不动别的号的那一行
            result.missing.append(_entry(game))
            continue
        uid = str(role.get("uid") or "").strip()
        target = find_account_row(rows, game, uid)
        created = target is None
        if created:
            target = _blank_row(game)
            rows.append(target)
        target["cookie"] = cookie
        for key in ("ltuid", "ltoken"):
            target.pop(key, None)
        target.setdefault("enabled", True)
        target.setdefault("id", new_row_id())
        if uid:
            target["uid"] = uid
        if role.get("server"):
            target["server"] = str(role["server"])
        if role.get("nickname"):
            target["nickname"] = str(role["nickname"])
        (result.added if created else result.updated).append(_entry(game, target))
    return result


def apply_skland_login(
    config: Any,
    token: str,
    games: Optional[List[str]] = None,
    roles: Optional[Dict[str, Dict[str, Any]]] = None,
) -> LoginWriteback:
    """把鹰角通行证 token 合并进森空岛两款的账号行。

    Args:
        config: ``{"accounts": rows}``（会被原地修改，调用方负责落盘）。
        token: 鹰角登录短 token。
        games: 这次只登哪几款；``None`` 表示两款都试。
        roles: ``{game: {uid, nickname}}``（登录时从 binding 顺带拉到）。
            拿不到身份时按「这款游戏只有一行就复用」处理。
    """
    rows = _account_rows(config)
    wanted = [game for game in SKLAND_GAMES if games is None or game in games]
    result = LoginWriteback()
    for game in wanted:
        role = roles.get(game) if isinstance(roles, dict) else None
        if roles:  # 拉到了身份表：没这款游戏的角色就不建行
            if not role:
                result.missing.append(_entry(game))
                continue
        uid = str((role or {}).get("uid") or "").strip()
        target = find_account_row(rows, game, uid)
        created = target is None
        if created:
            target = _blank_row(game)
            rows.append(target)
        target["token"] = token
        target.setdefault("enabled", True)
        target.setdefault("id", new_row_id())
        if uid:
            target["uid"] = uid
        if role and role.get("nickname"):
            target["nickname"] = str(role["nickname"])
        (result.added if created else result.updated).append(_entry(game, target))
    return result


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
    "PUSH_SCOPES",
    "SKLAND_GAMES",
    "LoginWriteback",
    "PluginSettings",
    "apply_mihoyo_login",
    "apply_skland_login",
    "find_account_row",
    "load",
    "mask_secret",
    "new_row_id",
    "parse_accounts",
    "parse_check_times",
    "parse_thresholds",
]
