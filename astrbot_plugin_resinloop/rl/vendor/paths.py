"""运行时目录解析。

上游 ResinLoop 用「源码树相对路径」存放运行状态（logs/、data/），桌面版没问题，
但 AstrBot 插件目录会被市场安装/更新覆盖，写在里面的状态会丢。

这里统一改成一个根目录，优先级：

1. 环境变量 ``RESINLOOP_DATA_DIR``（插件启动时按 AstrBot 数据目录注入，最优先）；
2. ``<插件目录>/.data``（脱离 AstrBot 单独跑测试时的兜底）。

所有模块都在 import 期读这些常量，所以插件必须在 import vendor 之前设好环境变量
（见 rl/bootstrap.py）。目录不存在时这里会顺手建出来。
"""

from __future__ import annotations

import os
from pathlib import Path

#: 根目录：设备指纹、凭证缓存、历史、活动缓存、推送历史都放它下面
ROOT_DIR = Path(
    os.environ.get("RESINLOOP_DATA_DIR")
    or (Path(__file__).resolve().parent.parent.parent / ".data")
).expanduser()

#: 业务数据（history / activities / push_history / cred_health / push_fail）
DATA_DIR = ROOT_DIR / "data"
#: 运行态缓存（device_identity / device_fp / skland_cred / 二维码图片）
LOGS_DIR = ROOT_DIR / "logs"

for _path in (ROOT_DIR, DATA_DIR, LOGS_DIR):
    try:
        _path.mkdir(parents=True, exist_ok=True)
    except OSError:  # 只读文件系统/权限不足时不要因为建目录把插件拖死
        pass

__all__ = ["ROOT_DIR", "DATA_DIR", "LOGS_DIR"]
