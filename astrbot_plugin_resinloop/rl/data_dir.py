"""运行时目录解析：所有状态落到 AstrBot 的插件数据目录。

引擎（rl/vendor）在 import 期就会读 ``RESINLOOP_DATA_DIR`` 建各种缓存路径，
所以这里一旦被导入就把环境变量钉好；``rl/__init__.py`` 保证它先于 vendor 执行。
"""

from __future__ import annotations

import os
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
_ENV_KEY = "RESINLOOP_DATA_DIR"


def _astrbot_data_root() -> Path | None:
    """AstrBot 的数据根目录（不同版本入口不同，逐个试）。"""
    for importer in (
        lambda: __import__(
            "astrbot.core.utils.astrbot_path", fromlist=["get_astrbot_data_path"]
        ).get_astrbot_data_path(),
        lambda: __import__(
            "astrbot.core.utils.astrbot_path", fromlist=["get_astrbot_root"]
        ).get_astrbot_root()
        / "data",
    ):
        try:
            value = importer()
            if value:
                return Path(value)
        except Exception:  # noqa: BLE001 - 老版本没有这个模块时静默降级
            continue
    return None


def _resolve() -> Path:
    preset = os.environ.get(_ENV_KEY)
    if preset:
        return Path(preset).expanduser()
    root = _astrbot_data_root()
    if root is not None:
        return root / "plugin_data" / "astrbot_plugin_resinloop"
    # 脱离 AstrBot 单跑的兜底：插件目录下 .data（已被 .gitignore 排除）
    return PLUGIN_DIR / ".data"


ROOT_DIR = _resolve()

try:
    ROOT_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

os.environ[_ENV_KEY] = str(ROOT_DIR)

__all__ = ["ROOT_DIR", "PLUGIN_DIR"]
