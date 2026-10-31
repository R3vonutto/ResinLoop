"""查询编排：把「配置」变成「一份检查报告」。

对应上游 ``main.py`` 里 run_once 的前半段（查询 → 凭证记账 → 活动缓存 → 判断），
推送这一段被换成了 AstrBot 会话推送（见 rl/pusher.py）。

全部是阻塞逻辑（requests + 线程池），对外只暴露一个 async 入口，
内部用 ``asyncio.to_thread`` 丢到线程里跑，别卡住 AstrBot 的事件循环。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .settings import PluginSettings
from .vendor import activities, cred_health, history, mihoyo_api
from .vendor.analyzer import DailyReport, build_report
from .vendor.config import AppConfig, MIHOYO_GAMES
from .vendor.models import GameStatus
from .vendor.querier import (
    UnknownProviderError,
    get_querier,
    set_mihoyo_task_scope,
)

logger = logging.getLogger("astrbot_plugin_resinloop.runner")

#: 凭证失效候选 retcode（与上游 main.py 同一份清单，禁自行增删）
_CRED_AUTH_RETCODES = frozenset({10001, -100, 1034, 5003, 10041})
_RETCODE_RE = re.compile(r"retcode=(-?\d+)")


@dataclass
class RunResult:
    """一轮检查的结果。"""

    report: DailyReport
    cred_alerts: List[Dict[str, Any]] = field(default_factory=list)
    duration: float = 0.0
    dry_run: bool = False
    skipped_reason: str = ""

    @property
    def all_failed(self) -> bool:
        return bool(self.report.statuses) and all(
            not item.ok for item in self.report.statuses
        )

    @property
    def need_alert(self) -> bool:
        return self.report.need_alert

    def push_payload(self) -> tuple[str, str]:
        return self.report.to_push_payload()


def apply_query_globals(app_config: AppConfig) -> None:
    """把代理 / 设备模式 / 任务口径下发给引擎，并注入活动日历的账号来源。

    必须在任何米游社请求之前调用：getFp 是按出口 IP 签发指纹的，
    代理只在部分请求生效等于没换 IP（上游踩过的坑，这里照搬同样的顺序）。
    """
    mihoyo_api.set_proxy(app_config.query.proxy)
    mihoyo_api.set_device_mode(app_config.query.mihoyo_device_mode)
    set_mihoyo_task_scope(app_config.query.mihoyo_task_scope)
    activities.set_account_provider(lambda: app_config.enabled_accounts)


def _query_account(account, provider: str) -> GameStatus:
    """查一个账号。任何异常都收口成带 error 的 GameStatus，绝不上抛。"""
    from .vendor.mihoyo_api import MiyousheAuthError, MiyousheNotBoundError

    try:
        querier = get_querier(account.game, provider=provider)
        return querier.fetch(account)
    except UnknownProviderError as exc:
        logger.error("查询器不可用：%s", exc)
        return GameStatus(
            game_id=account.game,
            game_name=account.game_name,
            nickname=account.nickname,
            error=str(exc),
            cred_kind="other",
        )
    except MiyousheNotBoundError as exc:
        logger.info("%s 未创建角色：%s", account.game_name, exc)
        return GameStatus(
            game_id=account.game,
            game_name=account.game_name,
            nickname=account.nickname,
            no_account=str(exc),
        )
    except MiyousheAuthError as exc:
        logger.error("%s 凭证失效：%s", account.game_name, exc)
        return GameStatus(
            game_id=account.game,
            game_name=account.game_name,
            nickname=account.nickname,
            error=str(exc),
            cred_kind="auth",
        )
    except Exception as exc:  # noqa: BLE001 - 兜底，单账号失败不影响整轮
        logger.exception("%s 查询异常", account.game_name)
        match = _RETCODE_RE.search(str(exc))
        cred_kind = (
            "auth" if (match and int(match.group(1)) in _CRED_AUTH_RETCODES) else "other"
        )
        return GameStatus(
            game_id=account.game,
            game_name=account.game_name,
            nickname=account.nickname,
            error=f"查询异常：{exc}",
            cred_kind=cred_kind,
        )


def collect_statuses(app_config: AppConfig) -> List[GameStatus]:
    """并行查询所有启用账号，结果顺序与配置一致。"""
    accounts = list(app_config.enabled_accounts)

    if app_config.query.provider == "miyoushe" and not app_config.query.mihoyo_enabled:
        skipped = [item for item in accounts if item.game in MIHOYO_GAMES]
        accounts = [item for item in accounts if item.game not in MIHOYO_GAMES]
        if skipped:
            logger.warning(
                "米游社查询已禁用（mihoyo_enabled=false），本轮跳过：%s",
                "、".join(item.game_name for item in skipped),
            )
    if not accounts:
        return []

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(
        max_workers=max(1, min(len(accounts), 8)), thread_name_prefix="resinloop"
    ) as pool:
        futures = [
            pool.submit(_query_account, account, app_config.query.provider)
            for account in accounts
        ]
        statuses = [future.result() for future in futures]

    for status in statuses:
        if not status.ok:
            detail = f"查询失败：{status.error}"
        elif status.no_account:
            detail = f"未创建角色：{status.no_account}"
        elif status.stale:
            detail = f"数据待更新：{status.stale}"
        elif status.finished:
            detail = "全部完成"
        else:
            detail = f"未完成 {len(status.unfinished_tasks)} 项"
        logger.info("%s(%s) -> %s", status.game_name, status.display_name, detail)
    return statuses


def _record_cred_health(app_config: AppConfig, statuses: List[GameStatus]) -> List[Dict[str, Any]]:
    """凭证健康记账：连续两次 auth 失败且 24h 内没提醒过 → 返回告警条目。"""
    alerts: List[Dict[str, Any]] = []
    accounts = list(app_config.enabled_accounts)
    game_seen: Dict[str, int] = {}
    for index, status in enumerate(statuses):
        same_game = [item for item in accounts if item.game == status.game_id]
        seen = game_seen.get(status.game_id, 0)
        game_seen[status.game_id] = seen + 1
        account = same_game[seen] if seen < len(same_game) else None
        key = f"#{index}:{status.game_id}"
        if account is not None:
            try:
                _ltuid, _ltoken, uid, _server, _cookie = mihoyo_api.extract_credentials(account)
                key = f"{uid}:{status.game_id}"
            except Exception:  # noqa: BLE001 - 森空岛/空凭证取不到 uid，退回昵称
                key = f"{account.nickname or '#' + str(index)}:{status.game_id}"
        alert = cred_health.record(
            key,
            not status.error,
            kind=status.cred_kind or ("other" if status.error else ""),
        )
        if alert:
            alerts.append(alert)
    return alerts


def _collect_activity_alerts(app_config: AppConfig) -> List[Any]:
    """活动结束提醒：每天最多真正请求一次，失败一路静默（不影响日常检查）。"""
    if not app_config.activity_alert_enabled:
        return []
    try:
        activities.ensure_fresh()
        alerts = activities.pending_alerts(hours=app_config.activity_alert_days * 24)
        enabled_ids = {item.game for item in app_config.enabled_accounts}
        return [alert for alert in alerts if alert.game_id in enabled_ids]
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取活动提醒失败（不影响本轮检查）：%s", exc)
        return []


def run_check_sync(settings: PluginSettings, *, dry_run: bool = False) -> RunResult:
    """跑一轮检查（阻塞）。没有可查账号时返回空报告，由调用方给提示。"""
    started = time.monotonic()
    app_config = settings.to_app_config()
    apply_query_globals(app_config)

    if not settings.ready and app_config.query.provider == "mock":
        return RunResult(
            report=DailyReport(),
            duration=time.monotonic() - started,
            dry_run=dry_run,
            skipped_reason="还没有绑定任何游戏账号",
        )

    statuses = collect_statuses(app_config)
    cred_alerts = _record_cred_health(app_config, statuses) if statuses else []
    activity_alerts = _collect_activity_alerts(app_config)
    report = build_report(
        statuses, app_config.resource_thresholds, activity_alerts=activity_alerts
    )

    if not dry_run:
        try:
            history.record(report)
        except Exception as exc:  # noqa: BLE001 - 存档失败不能影响主流程
            logger.warning("写入历史记录失败（不影响本轮检查）：%s", exc)

    logger.info(
        "汇总：总数=%d 已完成=%d 未完成=%d 失败=%d 漏做=%d 体力预警=%d 活动提醒=%d 耗时=%.1fs",
        len(report.statuses),
        len(report.finished),
        len(report.unfinished),
        len(report.failed),
        report.unfinished_task_count,
        len(report.resource_alerts),
        len(report.activity_alerts),
        time.monotonic() - started,
    )
    return RunResult(
        report=report,
        cred_alerts=cred_alerts,
        duration=time.monotonic() - started,
        dry_run=dry_run,
    )


async def run_check(settings: PluginSettings, *, dry_run: bool = False) -> RunResult:
    """异步入口：阻塞逻辑丢线程池，事件循环不被 HTTP 请求卡住。"""
    return await asyncio.to_thread(run_check_sync, settings, dry_run=dry_run)


def weekly_digest(days: int = 7) -> Dict[str, Any]:
    """最近 N 天完成情况（用于「周报」指令）。"""
    try:
        return history.weekly_digest(days)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取历史失败：%s", exc)
        return {}


__all__ = [
    "RunResult",
    "apply_query_globals",
    "collect_statuses",
    "run_check",
    "run_check_sync",
    "weekly_digest",
]
