"""按用户隔离的账号 / 订阅存储。

上游是单机程序，账号只有一份，存在插件配置里就够用。搬到群里就不行了：
配置是全局的，谁扫码谁覆盖，最后所有人都查同一个账号。

这里把「谁的账号」从插件配置里搬出来，落到插件数据目录的 ``users.json``：

    {
      "version": 1,
      "users": {
        "aiocqhttp:123456": {
          "platform": "aiocqhttp",
          "sender_id": "123456",
          "name": "群昵称",
          "accounts": [ {...账号行，与 WebUI template_list 同构...} ],
          "targets": ["aiocqhttp:FriendMessage:123456"],
          "private_umo": "aiocqhttp:FriendMessage:123456",
          "last_check": "10-31 18:20",
          "updated_at": "2026-10-31T18:20:11"
        }
      }
    }

设计要点：

* **账号行沿用 template_list 的结构**，这样 ``settings.parse_accounts`` 与
  ``apply_mihoyo_login`` / ``apply_skland_login`` 这些已有的回写逻辑可以原样复用，
  不必为「存文件」再写一套字段映射。
* **原子写**：先写 ``.tmp`` 再 ``os.replace``，进程被杀也不会留半截 JSON。
* **整文件读写 + 进程内 RLock**：单个机器人进程内不会有并发写，
  文件量级（几十个用户）也不值得上数据库。
* 文件里是明文凭证，与 AstrBot 自己的 ``config.json`` 同等敏感，
  插件数据目录不要提交、不要外发。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import data_dir
from . import settings as settings_mod
from .settings import GAME_LABELS, GAME_ORDER
from .vendor.config import AccountConfig

logger = logging.getLogger("astrbot_plugin_resinloop.userstore")

STORE_VERSION = 1
STORE_NAME = "users.json"

#: 一个用户最多绑多少个游戏（五款游戏 + 余量，防止有人用接口灌垃圾）
MAX_ACCOUNTS_PER_USER = 12


def user_key(platform: str, sender_id: str) -> str:
    """用户唯一标识：``平台:发送者ID``。

    跨群、跨私聊都是同一个人 —— 在 A 群绑定，去 B 群或私聊照样能查自己的，
    这正是「每个人独立」该有的样子。
    """
    platform = (platform or "").strip() or "unknown"
    sender = (sender_id or "").strip() or "unknown"
    return f"{platform}:{sender}"


def file_stem(key: str) -> str:
    """把用户 key 变成安全的文件名（Windows 不允许 ``:``，中文路径也别扭）。"""
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    readable = re.sub(r"[^0-9A-Za-z_.-]+", "_", key).strip("_")[:40]
    return f"{readable}.{digest}" if readable else digest


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class UserProfile:
    """一个用户的绑定情况（只读视图，改数据要走 UserStore）。"""

    key: str
    platform: str = ""
    sender_id: str = ""
    name: str = ""
    rows: List[Dict[str, Any]] = field(default_factory=list)
    targets: List[str] = field(default_factory=list)
    private_umo: str = ""
    last_check: str = ""
    updated_at: str = ""

    # ---- 账号 ----
    @property
    def accounts(self) -> List[AccountConfig]:
        return settings_mod.parse_accounts(self.rows)

    @property
    def enabled_accounts(self) -> List[AccountConfig]:
        return [item for item in self.accounts if item.enabled]

    @property
    def games(self) -> List[str]:
        seen: List[str] = []
        for account in self.accounts:
            if account.game not in seen:
                seen.append(account.game)
        return [game for game in GAME_ORDER if game in seen]

    @property
    def has_accounts(self) -> bool:
        return bool(self.enabled_accounts)

    @property
    def display(self) -> str:
        return self.name or self.sender_id or self.key

    def rows_for(self, game: str) -> Optional[Dict[str, Any]]:
        for row in self.rows:
            if str(row.get("game") or "").lower() == game:
                return row
        return None

    def summary_lines(self) -> List[str]:
        lines: List[str] = []
        for account in self.accounts:
            mark = "✓" if account.enabled else "✗"
            label = GAME_LABELS.get(account.game, account.game)
            who = account.nickname or "未命名"
            uid = str((account.credential or {}).get("uid") or "")
            lines.append(f"  {mark} {label}｜{who}" + (f"（UID {uid}）" if uid else ""))
        return lines


class UserStore:
    """users.json 的读写门面。所有方法都是线程安全的。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path else (data_dir.ROOT_DIR / STORE_NAME)
        self._lock = threading.RLock()
        self._data: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ #
    # 文件读写
    # ------------------------------------------------------------------ #

    @property
    def path(self) -> Path:
        return self._path

    def _empty(self) -> Dict[str, Any]:
        return {"version": STORE_VERSION, "users": {}}

    def _read(self) -> Dict[str, Any]:
        if self._data is not None:
            return self._data
        data: Dict[str, Any] = self._empty()
        try:
            if self._path.exists():
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    users = raw.get("users")
                    data["users"] = users if isinstance(users, dict) else {}
                    data["version"] = raw.get("version") or STORE_VERSION
        except Exception as exc:  # noqa: BLE001 - 文件写坏也要能继续跑，别让插件起不来
            logger.error("读取用户存储失败（按空处理）：%s", exc)
            backup = self._path.with_name(self._path.name + ".broken")
            try:
                self._path.replace(backup)
                logger.error("已把损坏的存储改名保留：%s", backup)
            except OSError:
                pass
        self._data = data
        return data

    def _flush(self) -> None:
        data = self._data or self._empty()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.error("写入用户存储失败：%s", exc)

    def _row(self, key: str, *, create: bool = False) -> Optional[Dict[str, Any]]:
        users = self._read()["users"]
        row = users.get(key)
        if row is None and create:
            platform, _, sender = key.partition(":")
            row = {
                "platform": platform,
                "sender_id": sender,
                "name": "",
                "accounts": [],
                "targets": [],
                "private_umo": "",
                "last_check": "",
                "created_at": _now_text(),
                "updated_at": _now_text(),
            }
            users[key] = row
        return row

    @staticmethod
    def _to_profile(key: str, row: Dict[str, Any]) -> UserProfile:
        accounts = row.get("accounts")
        targets = row.get("targets")
        return UserProfile(
            key=key,
            platform=str(row.get("platform") or ""),
            sender_id=str(row.get("sender_id") or ""),
            name=str(row.get("name") or ""),
            rows=accounts if isinstance(accounts, list) else [],
            targets=[str(item) for item in targets] if isinstance(targets, list) else [],
            private_umo=str(row.get("private_umo") or ""),
            last_check=str(row.get("last_check") or ""),
            updated_at=str(row.get("updated_at") or ""),
        )

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #

    def profile(self, key: str) -> UserProfile:
        with self._lock:
            row = self._row(key) or {}
            return self._to_profile(key, row)

    def profiles(self) -> List[UserProfile]:
        with self._lock:
            users = self._read()["users"]
            items = [self._to_profile(key, row) for key, row in users.items()]
        return [item for item in items if isinstance(item.rows, list)]

    def bindable_profiles(self) -> List[UserProfile]:
        """有账号、且至少有一个推送去处（订阅会话或私聊）的用户。"""
        return [
            item for item in self.profiles()
            if item.has_accounts and (item.targets or item.private_umo)
        ]

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #

    def touch(
        self,
        key: str,
        *,
        name: str = "",
        platform: str = "",
        sender_id: str = "",
    ) -> UserProfile:
        """记录/更新用户身份信息（昵称会随群名片变化，每次命令都刷新）。"""
        with self._lock:
            row = self._row(key, create=True)
            assert row is not None
            changed = False
            if name and row.get("name") != name:
                row["name"] = name
                changed = True
            if platform and row.get("platform") != platform:
                row["platform"] = platform
                changed = True
            if sender_id and row.get("sender_id") != sender_id:
                row["sender_id"] = sender_id
                changed = True
            if changed:
                row["updated_at"] = _now_text()
                self._flush()
            return self._to_profile(key, row)

    def set_private_umo(self, key: str, umo: str) -> None:
        """记住这个用户的私聊会话，定时提醒优先发这里（群里不串号）。"""
        if not umo:
            return
        with self._lock:
            row = self._row(key, create=True)
            assert row is not None
            if row.get("private_umo") == umo:
                return
            row["private_umo"] = umo
            row["updated_at"] = _now_text()
            self._flush()

    def set_last_check(self, key: str, stamp: str) -> None:
        with self._lock:
            row = self._row(key)
            if row is None:
                return
            row["last_check"] = stamp
            row["updated_at"] = _now_text()
            self._flush()

    def set_accounts(self, key: str, rows: List[Dict[str, Any]]) -> None:
        with self._lock:
            row = self._row(key, create=True)
            assert row is not None
            row["accounts"] = rows[:MAX_ACCOUNTS_PER_USER]
            row["updated_at"] = _now_text()
            self._flush()

    def login_rows(self, key: str) -> List[Dict[str, Any]]:
        """取一个可原地修改的账号行列表，交给 apply_*_login 回写。

        返回的是存储里的真实列表对象：``settings.apply_mihoyo_login`` 会就地改它，
        改完调用 :meth:`save_login_rows` 落盘即可。
        """
        with self._lock:
            row = self._row(key, create=True)
            assert row is not None
            rows = row.get("accounts")
            if not isinstance(rows, list):
                rows = []
                row["accounts"] = rows
            return rows

    def save_login_rows(self, key: str) -> None:
        with self._lock:
            row = self._row(key)
            if row is not None:
                row["updated_at"] = _now_text()
                self._flush()

    def unbind(self, key: str, game: str = "") -> List[str]:
        """解绑单个游戏（``game`` 为空则清空全部），返回被移除的中文游戏名。"""
        with self._lock:
            row = self._row(key)
            removed: List[str] = []
            if row is None:
                return removed
            rows = row.get("accounts")
            if not isinstance(rows, list):
                return removed
            if not game:
                removed = [
                    GAME_LABELS.get(str(item.get("game") or ""), str(item.get("game") or ""))
                    for item in rows
                    if isinstance(item, dict)
                ]
                row["accounts"] = []
            else:
                kept: List[Dict[str, Any]] = []
                for item in rows:
                    if isinstance(item, dict) and str(item.get("game") or "").lower() == game:
                        removed.append(GAME_LABELS.get(game, game))
                    else:
                        kept.append(item)
                row["accounts"] = kept
            row["updated_at"] = _now_text()
            self._flush()
            return removed

    def subscribe(self, key: str, umo: str) -> bool:
        """订阅一个会话；返回 True 表示本次是新增。"""
        if not umo:
            return False
        with self._lock:
            row = self._row(key, create=True)
            assert row is not None
            targets = row.get("targets")
            if not isinstance(targets, list):
                targets = []
                row["targets"] = targets
            if umo in targets:
                return False
            targets.append(umo)
            row["updated_at"] = _now_text()
            self._flush()
            return True

    def unsubscribe(self, key: str, umo: str) -> bool:
        with self._lock:
            row = self._row(key)
            if row is None:
                return False
            targets = row.get("targets")
            if not isinstance(targets, list) or umo not in targets:
                return False
            row["targets"] = [item for item in targets if item != umo]
            row["updated_at"] = _now_text()
            self._flush()
            return True

    # ------------------------------------------------------------------ #
    # 旧版共享账号迁移
    # ------------------------------------------------------------------ #

    def claim_legacy(
        self, key: str, rows: List[Dict[str, Any]], targets: Optional[List[str]] = None
    ) -> int:
        """把配置里的旧账号并入某个用户名下（同游戏以新绑定为准，不覆盖）。"""
        if not rows:
            return 0
        with self._lock:
            row = self._row(key, create=True)
            assert row is not None
            current = row.get("accounts")
            if not isinstance(current, list):
                current = []
            existing = {str(item.get("game") or "").lower() for item in current if isinstance(item, dict)}
            added = 0
            for item in rows:
                if not isinstance(item, dict):
                    continue
                game = str(item.get("game") or "").lower()
                if not game or game in existing:
                    continue
                current.append(dict(item))
                existing.add(game)
                added += 1
            row["accounts"] = current[:MAX_ACCOUNTS_PER_USER]
            if targets:
                merged = list(row.get("targets") or [])
                for target in targets:
                    if target and target not in merged:
                        merged.append(target)
                row["targets"] = merged
            row["updated_at"] = _now_text()
            self._flush()
            return added

    # ------------------------------------------------------------------ #
    # 统计
    # ------------------------------------------------------------------ #

    def stats(self) -> Dict[str, int]:
        profiles = self.profiles()
        return {
            "users": len(profiles),
            "bound": sum(1 for item in profiles if item.has_accounts),
            "subscribed": sum(1 for item in profiles if item.targets or item.private_umo),
        }


__all__ = [
    "MAX_ACCOUNTS_PER_USER",
    "STORE_NAME",
    "UserProfile",
    "UserStore",
    "file_stem",
    "user_key",
]
