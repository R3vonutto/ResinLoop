"""回复文案。

集中在一处，是为了让 main.py 只管「命令 → 数据 → 发消息」，不掺排版。
聊天窗口窄，所有列表都做长度收敛：宁可少说两句，也不要发一大坨。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from .settings import GAME_LABELS, PluginSettings
from .vendor import activities

LINE = "─" * 24
#: 手动检查回复最多带多少行（超了容易在手机上刷屏）
MAX_REPORT_LINES = 46


def help_text(settings: PluginSettings, *, bound: bool = False) -> str:
    prefix = "/日常"
    lines = [
        "🌱 ResinLoop · 游戏日常巡查",
        LINE,
        f"{prefix}            立即检查【你自己的】账号",
        f"{prefix} 状态       你的绑定 / 提醒设置",
        f"{prefix} 周报       你自己最近 7 天的完成情况",
        f"{prefix} 活动       即将结束的活动",
        f"{prefix} 登录米游社  扫码绑定原神 / 崩铁 / 绝区零",
        f"{prefix} 登录森空岛  扫码绑定明日方舟 / 终末地",
        f"{prefix} 订阅       在【当前会话】接收你的定时提醒",
        f"{prefix} 退订       取消当前会话的提醒",
        f"{prefix} 解绑 [游戏] 删除你自己的绑定（不给参数则全删）",
        f"{prefix} 测试推送    按当前结果给你自己推一条",
        LINE,
        "账号按「平台 + 用户」各自独立，群里每个人查到的都是自己那份。",
        "只查询、只提醒：不签到、不领奖励、不改任何游戏数据。",
    ]
    if not bound:
        lines.append("现在还没有绑定，先来一句：" + f"{prefix} 登录米游社")
    return "\n".join(lines)


def binding_hint(prefix: str = "/日常") -> str:
    return (
        "你还没有绑定任何游戏账号。\n"
        f"私聊我发「{prefix} 登录米游社」或「{prefix} 登录森空岛」扫码即可，"
        "绑定只对你自己生效。"
    )


def status_text(
    settings: PluginSettings,
    *,
    profile: Any = None,
    last_run: str = "",
    next_run: Optional[datetime] = None,
    scheduler_running: bool = False,
    stats: Optional[Dict[str, int]] = None,
) -> str:
    """个人状态：只看自己的账号与提醒设置，末尾附一行全局人数。"""
    lines: List[str] = []
    if profile is not None:
        lines.append(f"📋 我的状态｜{profile.display}")
    else:
        lines.append("📋 ResinLoop 状态")
    lines.append(LINE)

    accounts = list(getattr(profile, "accounts", []) or []) if profile is not None else list(settings.accounts)
    if not accounts:
        lines.append("账号：还没有绑定（/日常 登录米游社）")
    else:
        enabled = [item for item in accounts if item.enabled]
        lines.append(f"账号（{len(enabled)}/{len(accounts)} 启用）：")
        for account in accounts:
            mark = "✓" if account.enabled else "✗"
            label = GAME_LABELS.get(account.game, account.game)
            who = account.nickname or "未命名"
            uid = str((account.credential or {}).get("uid") or "")
            lines.append(f"  {mark} {label}｜{who}" + (f"（UID {uid}）" if uid else ""))

    lines.append("")
    if profile is not None:
        private = "已记住，提醒优先发私聊" if getattr(profile, "private_umo", "") else "还没私聊过我"
        lines.append(f"私聊：{private}")
        targets = list(getattr(profile, "targets", []) or [])
        lines.append(f"订阅会话：{len(targets)} 个")
        for target in targets[:3]:
            lines.append(f"  · {target}")
        if len(targets) > 3:
            lines.append(f"  · …共 {len(targets)} 个")
    else:
        lines.append(f"推送会话：{len(settings.push_targets)} 个")
        for target in settings.push_targets[:3]:
            lines.append(f"  · {target}")

    lines.append("")
    lines.append(f"定时：{'、'.join(settings.check_times) or '未设置'}")
    lines.append(
        f"调度：{'运行中' if scheduler_running else '已停止'}"
        + (f"，下次 {next_run:%m-%d %H:%M}" if next_run else "")
    )
    stamp = getattr(profile, "last_check", "") or last_run
    if stamp:
        lines.append(f"上次检查：{stamp}")

    alert_parts: List[str] = []
    if settings.resource_alert_enabled:
        for game in ("genshin", "starrail", "zzz", "arknights", "endfield"):
            threshold = settings.thresholds.get(game) or 0
            if threshold > 0:
                alert_parts.append(f"{GAME_LABELS[game]}≥{threshold}")
    lines.append("体力预警：" + ("、".join(alert_parts) if alert_parts else "关闭"))

    if stats:
        lines.append(
            f"全局：{stats.get('bound', 0)} 人已绑定 / {stats.get('users', 0)} 人用过插件"
        )
    return "\n".join(lines)


def cooldown_text(seconds: int) -> str:
    return f"刚查过，{max(1, int(seconds))} 秒后再试（防止把接口打爆）。"


def cache_note(age_seconds: int) -> str:
    age = max(0, int(age_seconds))
    when = "刚刚" if age < 5 else f"{age} 秒前"
    return f"\n（这是{when}的缓存结果，/日常 测试推送 可强制刷新）"


def unbind_text(removed: List[str], remain: int) -> str:
    if not removed:
        return "没有找到要解绑的账号。"
    text = "已解绑：" + "、".join(removed)
    if remain:
        return text + f"\n还剩 {remain} 个账号。"
    return text + "\n账号已清空，随时可以重新扫码绑定。"


def claim_text(added: int) -> str:
    if added <= 0:
        return "配置里没有可认领的旧账号（或这些游戏你都已经绑过了）。"
    return (
        f"已把配置里的 {added} 个旧账号并入你名下，并把配置清空。\n"
        "以后的日常检查只认你自己的绑定，别人看不到。"
    )


def _truncate(text: str, max_lines: int = MAX_REPORT_LINES) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    kept = lines[:max_lines]
    kept.append(f"…（还有 {len(lines) - max_lines} 行，详见日志）")
    return "\n".join(kept)


def report_text(report: Any, *, title: str = "") -> str:
    """把 DailyReport 渲染成聊天可读的报告（复用引擎的控制台排版）。"""
    try:
        body = report.to_console_text()
    except Exception:  # noqa: BLE001 - 渲染异常不该让命令失败
        body = "报告渲染失败，请查看插件日志。"
    text = f"{title}\n{body}" if title else body
    return _truncate(text)


def push_text(report: Any) -> str:
    """推送文案与手机通知一致（标题 + 正文）。"""
    try:
        title, body = report.to_push_payload()
    except Exception:  # noqa: BLE001
        return "游戏日常：报告渲染失败，请查看插件日志。"
    return f"{title}\n{body}"



def weekly_text(digest: Dict[str, Any]) -> str:
    games = digest.get("games") or []
    days_count = int(digest.get("days_count") or 0)
    if not games:
        return "📊 最近一周还没有巡检记录。先手动检查一次（/日常）再来看看。"

    lines = [f"📊 最近 {days_count} 天完成情况", LINE]
    for item in games[:12]:
        known = int(item.get("known_days") or 0)
        done = int(item.get("done_days") or 0)
        rate = (done / known * 100) if known else 0
        bar = "█" * round(rate / 10) + "░" * (10 - round(rate / 10))
        lines.append(
            f"{item.get('name') or item.get('game')}：{bar} {done}/{known}（{rate:.0f}%）"
        )
    missed = [
        item for item in games if int(item.get("missed_days") or 0) > 0
    ]
    if missed:
        worst = max(missed, key=lambda item: int(item.get("missed_days") or 0))
        lines.append("")
        lines.append(
            f"最需要盯的是 {worst.get('name')}：漏了 {worst.get('missed_days')} 天。"
        )
    else:
        lines.append("")
        lines.append("一周零漏做，可以放心摆烂。")
    return "\n".join(lines)


def activity_text(settings: PluginSettings, limit: int = 12) -> str:
    """活动列表：进行中按结束时间排序，未开启的单独一句带过。"""
    if not settings.activity_alert_enabled:
        return "活动提醒已在配置里关闭。"
    try:
        activities.ensure_fresh()
        items = activities.ongoing()
    except Exception as exc:  # noqa: BLE001
        return f"读取活动日历失败：{exc}"

    enabled = set(settings.game_ids)
    items = [item for item in items if item.game_id in enabled] if enabled else items
    if not items:
        return "最近没有需要关注的活动（或活动日历还没拉到数据）。"

    ongoing = [item for item in items if not item.not_started]
    upcoming = [item for item in items if item.not_started]
    lines = ["📅 活动日历", LINE]
    for item in ongoing[:limit]:
        lines.append(f"· {item.game_name}｜{item.title}")
        extra = item.countdown_text()
        if item.progress:
            extra = f"{extra}｜{item.progress}"
        lines.append(f"    {extra}")
    if len(ongoing) > limit:
        lines.append(f"…还有 {len(ongoing) - limit} 个进行中活动")
    if upcoming:
        lines.append("")
        lines.append(
            "未开启：" + "、".join(f"{item.game_name}·{item.title}" for item in upcoming[:5])
        )
    return "\n".join(lines)


def login_done_text(kind: str, games: List[str], extra: str = "") -> str:
    label = "米游社" if kind == "mihoyo" else "鹰角通行证"
    game_text = "、".join(games) if games else "（没有可写入的账号行）"
    lines = [f"✅ {label}登录成功，已写入：{game_text}"]
    if extra:
        lines.append(extra)
    lines.append("现在可以 /日常 立即检查一轮。")
    return "\n".join(lines)


def cred_alert_text(alerts: List[Dict[str, Any]]) -> str:
    names = "、".join(
        GAME_LABELS.get(str(item.get("game_id") or ""), str(item.get("game_id") or ""))
        for item in alerts
    )
    return (
        f"🔑 凭证可能失效：{names}\n"
        "已连续多次查询失败（凭证类），请重新扫码登录（/日常 登录米游社 或 /日常 登录森空岛）。"
    )


__all__ = [
    "activity_text",
    "binding_hint",
    "cache_note",
    "claim_text",
    "cooldown_text",
    "cred_alert_text",
    "help_text",
    "login_done_text",
    "push_text",
    "report_text",
    "status_text",
    "unbind_text",
    "weekly_text",
]
