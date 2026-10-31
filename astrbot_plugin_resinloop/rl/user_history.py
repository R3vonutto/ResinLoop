"""按用户隔离的历史存档。

引擎的 ``history`` 模块把「每天每款游戏做没做完」写到固定的 ``history.json``，
单机程序这样正好；多用户就必须一人一份，否则周报会把别人的记录算进来。

vendor 里的代码不重写（它是原样移植的部分），这里用最小侵入的方式复用：
历史模块的所有读写都在**函数调用时**读模块级 ``HISTORY_PATH``，
所以「临时把路径指向该用户的文件」就能整体切走，用完再换回来。

约束与代价：

* 这是进程级的状态切换，必须串行。所有调用点都在 ``run_check`` 的全局锁内，
  或通过 :func:`scoped` 自己的可重入锁排队，不会交叉。
* 只在 ``rl`` 这一层用，vendor 里没有任何人知道这件事。

用法：

    with scoped(path_for(key)):
        history.record(report)
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from . import data_dir
from .userstore import file_stem
from .vendor import history

logger = logging.getLogger("astrbot_plugin_resinloop.history")

HISTORY_DIR = data_dir.ROOT_DIR / "history"

#: 保护 HISTORY_PATH 的切换（线程池里并发调用会互相踩）
_LOCK = threading.RLock()


def path_for(key: str) -> Path:
    """某个用户的历史文件路径（键为空时退回全局文件）。"""
    name = file_stem(key) if key else "shared"
    return HISTORY_DIR / f"{name}.json"


@contextmanager
def scoped(path: Optional[Path]) -> Iterator[Path]:
    """在该路径的历史文件上执行一段逻辑。``path`` 为空时用引擎默认路径。"""
    if path is None:
        yield history.HISTORY_PATH
        return
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # noqa: BLE001 - 建不了目录就退回全局，至少不崩
        logger.warning("创建历史目录失败，退回全局存档：%s", exc)
        yield history.HISTORY_PATH
        return

    with _LOCK:
        previous = history.HISTORY_PATH
        history.HISTORY_PATH = target
        try:
            yield target
        finally:
            history.HISTORY_PATH = previous


def migrate_shared_to(key: str) -> bool:
    """把旧的全局存档复制成某个用户的存档（认领账号时顺手做）。

    旧文件在引擎的 ``DATA_DIR``（``<数据根>/data/history.json``），新文件在
    ``<数据根>/history/<用户>.json``，两份格式相同，直接复制即可。
    """
    source = history.DATA_DIR / "history.json"
    if not source.exists():
        return False
    target = path_for(key)
    if target.exists():
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        logger.info("旧存档已复制给 %s：%s", key, target.name)
        return True
    except OSError as exc:  # noqa: BLE001
        logger.warning("复制旧存档失败：%s", exc)
        return False


__all__ = ["HISTORY_DIR", "migrate_shared_to", "path_for", "scoped"]
