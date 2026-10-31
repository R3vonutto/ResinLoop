"""ResinLoop → AstrBot 适配层。

目录约定：

    rl/data_dir.py       运行时目录解析（必须在 import vendor 之前跑完）
    rl/settings.py       插件配置 → 引擎需要的强类型对象
    rl/userstore.py      按用户隔离的账号 / 订阅存储（users.json）
    rl/user_history.py   按用户隔离的历史存档（复用引擎的 history 逻辑）
    rl/runner.py         查询 + 判断（阻塞逻辑，由调用方丢进线程）
    rl/pusher.py         推送（AstrBot 会话 + 可选 Webhook）
    rl/scheduler.py      定时检查（含补发）
    rl/login.py          扫码登录（米游社 / 鹰角通行证）
    rl/vendor/           原仓库 core/ 的原样移植（只改了目录与导入方式）
"""

from . import data_dir  # noqa: F401  —— 导入即设置 RESINLOOP_DATA_DIR

__all__ = ["data_dir"]
