"""扫码登录：米游社（原神/崩铁/绝区零）与鹰角通行证（明日方舟/终末地）。

上游是「终端里打印 ASCII 二维码 → 阻塞轮询」，AstrBot 里换成
「生成二维码 PNG 发到聊天 → 后台线程轮询 → 完成后回一条消息」。

拆成 begin / finish 两段，是为了让插件在中间把图片发出去。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .vendor import hypergryph_login, mihoyo_api, mihoyo_login, skland_api
from .vendor.paths import LOGS_DIR

logger = logging.getLogger("astrbot_plugin_resinloop.login")

#: 扫码等待上限（秒）
DEFAULT_TIMEOUT = 180

#: 森空岛 binding 里的 appCode → 我们的游戏标识（与 skland_api 一致）
SKLAND_APPS = ("arknights", "endfield")


class LoginError(Exception):
    """扫码登录过程中的可读错误（网络 / 过期 / 取消 / 字段缺失）。"""


@dataclass
class QrSession:
    """一次扫码会话：发图用 png，轮询用内部字段。"""

    kind: str                      # "mihoyo" / "skland"
    url: str                       # 二维码内容（渲染失败时直接给用户这串）
    png: Optional[Path] = None     # 本地二维码图片
    ticket: str = ""               # 米游社 ticket
    scan_id: str = ""              # 鹰角 scanId
    device_id: str = ""            # 米游社扫码设备 id
    #: 这次只登哪几款游戏（空 = 全部）。只影响「把凭证写到哪几行」，
    #: 扫码本身由手机上的账号决定 —— 所以同一个通行证下没玩过的游戏会被跳过。
    games: Tuple[str, ...] = ()

    def hint(self) -> str:
        if self.kind == "mihoyo":
            return "请用手机【米游社 App】扫码并确认登录"
        return "请用手机上的【森空岛 / 明日方舟 / 终末地 App】扫码并确认登录"


def render_qr_png(content: str, path: Path) -> Path:
    """把一段内容渲染成二维码 PNG。

    qrcode 没装时抛 LoginError，由调用方降级成「直接把链接发给用户」。
    """
    try:
        import qrcode  # noqa: PLC0415 - 可选依赖，用到才导入
    except ImportError as exc:  # pragma: no cover - 取决于部署环境
        raise LoginError(
            "缺少 qrcode 依赖，无法生成二维码图片。请先安装 requirements.txt 里的依赖，"
            "或直接把下面的链接转成二维码：\n" + content
        ) from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    qr = qrcode.QRCode(border=2, box_size=8)
    qr.add_data(content)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    image.save(path)
    return path


# --------------------------------------------------------------------------- #
# 米游社
# --------------------------------------------------------------------------- #


def _qr_path(kind: str, tag: str) -> Path:
    """二维码落盘路径。多人同时扫码时靠 ``tag`` 区分，不会互相覆盖。"""
    suffix = f"_{tag}" if tag else ""
    return LOGS_DIR / f"qr_{kind}{suffix}.png"


async def begin_mihoyo(tag: str = "", games: Sequence[str] = ()) -> QrSession:
    """创建米游社扫码会话并渲染二维码。"""
    try:
        device_id = mihoyo_api.identity_device_id()
        session = await asyncio.to_thread(mihoyo_login.create_qr, device_id)
    except Exception as exc:  # noqa: BLE001 - 网络/retcode 都收敛成可读错误
        raise LoginError(f"创建米游社二维码失败：{exc}") from exc

    png: Optional[Path] = None
    try:
        png = await asyncio.to_thread(
            render_qr_png, session["url"], _qr_path("mihoyo", tag)
        )
    except LoginError:
        png = None
    return QrSession(
        kind="mihoyo",
        url=session["url"],
        png=png,
        ticket=session["ticket"],
        device_id=device_id,
        games=tuple(games or ()),
    )


async def finish_mihoyo(
    session: QrSession, timeout: int = DEFAULT_TIMEOUT
) -> Dict[str, Any]:
    """等待扫码确认，返回 {cookie, ltuid, ltoken, device_id, roles}。"""
    try:
        result = await asyncio.to_thread(
            mihoyo_login.poll_confirm, session.device_id, session.ticket, timeout
        )
    except Exception as exc:  # noqa: BLE001
        raise LoginError(str(exc)) from exc

    try:
        cookie, creds = mihoyo_login.build_cookie_string(result["cookies"])
    except Exception as exc:  # noqa: BLE001
        raise LoginError(str(exc)) from exc

    # 绑定设备指纹：解决 5003 风控的关键一步，失败也不阻断（凭证本身已经可用）
    try:
        await asyncio.to_thread(mihoyo_api.bind_device, session.device_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("设备指纹绑定失败（不影响登录）：%s", exc)

    # 拉角色列表回填 uid / server / 昵称，失败就保持旧配置
    roles: Dict[str, Dict[str, Any]] = {}
    try:
        roles = await asyncio.to_thread(
            mihoyo_api.fetch_game_roles, creds["ltuid"], creds["ltoken"], cookie
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("角色列表获取失败（不回填 uid，可在日志里查）：%s", exc)

    return {
        "cookie": cookie,
        "ltuid": creds["ltuid"],
        "ltoken": creds["ltoken"],
        "device_id": session.device_id,
        "roles": roles,
    }


# --------------------------------------------------------------------------- #
# 鹰角通行证（森空岛）
# --------------------------------------------------------------------------- #


async def begin_skland(tag: str = "", games: Sequence[str] = ()) -> QrSession:
    """创建鹰角通行证扫码会话并渲染二维码。"""
    try:
        session = await asyncio.to_thread(hypergryph_login.create_qr)
    except Exception as exc:  # noqa: BLE001
        raise LoginError(f"创建鹰角通行证二维码失败：{exc}") from exc

    png: Optional[Path] = None
    try:
        png = await asyncio.to_thread(
            render_qr_png, session["scan_url"], _qr_path("skland", tag)
        )
    except LoginError:
        png = None
    return QrSession(
        kind="skland",
        url=session["scan_url"],
        png=png,
        scan_id=session["scan_id"],
        games=tuple(games or ()),
    )


def validate_skland_token(token: str) -> str:
    """登录 token 格式体检，返回 '' 表示通过（与上游 main.py 同一套判据）。"""
    token = (token or "").strip()
    if not token:
        return "登录接口返回空 token"
    if any(char.isspace() for char in token):
        return f"token 含空白字符（长度 {len(token)}），疑似格式污染"
    if token.startswith("eyJ"):
        return "拿到的是网页端 JWT（以 eyJ 开头），skland 只接受登录短 token"
    if len(token) > skland_api.MAX_TOKEN_LEN:
        return f"token 长度 {len(token)} 超过接口上限 {skland_api.MAX_TOKEN_LEN}"
    if len(token) < 16:
        return f"token 过短（长度 {len(token)}）"
    return ""


def skland_roles(ark_token: str) -> Dict[str, Dict[str, str]]:
    """用刚拿到的鹰角 token 读一次 binding，返回 ``{game: {uid, nickname}}``。

    用途是「认人」：同一个手机号再登一次要刷新原来那行，另一个手机号要新增一行，
    只看 token 分不出来（每次扫码都是新 token），得靠游戏内的 uid。
    终末地没有独立 uid，退化成 ``defaultRole.roleId``。

    这一步失败不影响登录本身（凭证已经有效），只是回写时会退化成
    「这款游戏只有一行就复用」的保守策略。
    """
    credential = skland_api.get_skland_credentials({"token": ark_token}, SKLAND_APPS[0])
    players = skland_api.list_players(credential["cred"], credential["token"])
    roles: Dict[str, Dict[str, str]] = {}
    entries = players.get("list")
    if not isinstance(entries, list):
        return roles
    for app in entries:
        if not isinstance(app, dict):
            continue
        code = str(app.get("appCode") or "").strip()
        if code not in SKLAND_APPS:
            continue
        bindings = app.get("bindingList")
        if not isinstance(bindings, list) or not bindings:
            continue
        binding = bindings[0] if isinstance(bindings[0], dict) else {}
        role = binding.get("defaultRole")
        role = role if isinstance(role, dict) else {}
        uid = str(binding.get("uid") or role.get("roleId") or "").strip()
        nickname = str(role.get("nickname") or binding.get("nickName") or "").strip()
        roles[code] = {"uid": uid, "nickname": nickname}
    return roles


async def finish_skland(
    session: QrSession, timeout: int = DEFAULT_TIMEOUT
) -> Dict[str, Any]:
    """等待扫码确认并换取登录 token，返回 {token, roles}。"""
    try:
        scan_code = await asyncio.to_thread(
            hypergryph_login.poll_confirm, session.scan_id, timeout
        )
        token = await asyncio.to_thread(hypergryph_login.exchange_short_token, scan_code)
    except Exception as exc:  # noqa: BLE001
        raise LoginError(str(exc)) from exc

    problem = validate_skland_token(token)
    if problem:
        raise LoginError(f"登录成功但 token 校验未通过：{problem}")

    check = await asyncio.to_thread(skland_api.verify_ark_token, token)
    if not check.get("ok"):
        raise LoginError(f"token 未通过鹰角 grant 校验：{check.get('error')}")

    roles: Dict[str, Dict[str, str]] = {}
    try:
        roles = await asyncio.to_thread(skland_roles, token)
    except Exception as exc:  # noqa: BLE001 - 认不出身份也要让登录成功
        logger.warning("森空岛角色列表获取失败（不影响登录，回写时按保守策略处理）：%s", exc)
    return {"token": token, "roles": roles}


__all__ = [
    "DEFAULT_TIMEOUT",
    "SKLAND_APPS",
    "LoginError",
    "QrSession",
    "begin_mihoyo",
    "begin_skland",
    "finish_mihoyo",
    "finish_skland",
    "render_qr_png",
    "skland_roles",
    "validate_skland_token",
]
