"""回复文案。

集中在一处，是为了让 main.py 只管「命令 → 数据 → 发消息」，不掺排版。
聊天窗口窄，所有列表都做长度收敛：宁可少说两句，也不要发一大坨。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from .settings import GAME_LABELS, PluginSettings
from .vendor import activities

#: 手动检查回复最多带多少行（超了容易在手机上刷屏）
MAX_REPORT_LINES = 46


def help_text(settings: PluginSettings, *, bound: bool = False) -> str:
    prefix = "/日常"
    lines = [
        "🌱 ResinLoop 游戏日常",
        "",
        f"{prefix} 检查我的账号",
        f"{prefix} 状态 绑定与提醒",
        f"{prefix} 周报 近 7 天完成情况",
        f"{prefix} 活动 即将结束的活动",
        f"{prefix} 登录米游社 原神 / 崩铁 / 绝区零",
        f"{prefix} 登录森空岛 明日方舟 / 终末地",
        f"{prefix} 别名 2 小号 给账号起名",
        f"{prefix} 解绑 2 删掉 2 号账号（「全部」清空）",
        f"{prefix} 订阅 本会话接收提醒",
        f"{prefix} 退订 取消本会话提醒",
        f"{prefix} 测试推送 推一条到我的提醒",
        "",
        "一个游戏可以绑多个号：再登一次就行。",
    ]
    return "\n".join(lines)


def binding_hint(prefix: str = "/日常") -> str:
    return f"还没绑定账号。\n发 {prefix} 登录米游社 或 {prefix} 登录森空岛 扫码绑定。"


def status_text(
    settings: PluginSettings,
    *,
    profile: Any = None,
    last_run: str = "",
    next_run: Optional[datetime] = None,
    scheduler_running: bool = False,
) -> str:
    """个人状态：只看自己的账号与提醒设置。"""
    lines: List[str] = []
    if profile is not None:
        lines.append(f"📋 我的状态 {profile.display}")
    else:
        lines.append("📋 ResinLoop 状态")

    entries = list(getattr(profile, "entries", []) or []) if profile is not None else []
    if profile is not None and not entries:
        lines.append("账号：还没绑定（/日常 登录米游社）")
    elif entries:
        enabled = sum(1 for item in entries if item.enabled)
        lines.append(f"账号 {enabled}/{len(entries)}：")
        lines.extend(account_lines(entries, with_uid=True))
    else:
        accounts = list(settings.accounts)
        enabled = [item for item in accounts if item.enabled]
        lines.append(f"账号 {len(enabled)}/{len(accounts)}：")
        for account in accounts:
            mark = "✓" if account.enabled else "✗"
            label = GAME_LABELS.get(account.game, account.game)
            who = account.nickname or "未命名"
            uid = str((account.credential or {}).get("uid") or "")
            lines.append(f"  {mark} {label}（{who}）" + (f" UID {uid}" if uid else ""))

    lines.append("")
    if profile is not None:
        targets = list(getattr(profile, "targets", []) or [])
        lines.append(f"订阅会话：{len(targets)} 个")
    else:
        lines.append(f"订阅会话：{len(settings.push_targets)} 个")

    schedule = f"定时 {'、'.join(settings.check_times) or '未设置'}"
    if next_run:
        schedule += f"，下次 {next_run:%m-%d %H:%M}"
    stamp = getattr(profile, "last_check", "") or last_run
    if stamp:
        schedule += f"，上次 {stamp}"
    lines.append(schedule)
    if not scheduler_running:
        lines.append("调度已停止（插件未启用或无检查时间点）")

    alert_parts: List[str] = []
    if settings.resource_alert_enabled:
        # 只列自己绑了的游戏，避免五行阈值全是别人的事
        mine = {item.game for item in (entries or settings.accounts)} or set()
        for game in ("genshin", "starrail", "zzz", "arknights", "endfield"):
            if mine and game not in mine:
                continue
            threshold = settings.thresholds.get(game) or 0
            if threshold > 0:
                alert_parts.append(f"{GAME_LABELS[game]}≥{threshold}")
    lines.append("体力预警：" + ("、".join(alert_parts) if alert_parts else "关闭"))
    return "\n".join(lines)


def account_lines(entries: List[Any], *, with_uid: bool = False) -> List[str]:
    """账号清单：带编号（解绑/起别名要用），同名两个号靠别名区分。"""
    lines: List[str] = []
    for entry in entries:
        mark = "✓" if entry.enabled else "✗"
        text = f"  {entry.index} {mark} {entry.name}"
        if with_uid and entry.uid:
            text += f" UID {entry.uid}"
        lines.append(text)
    return lines


def login_menu_text(
    kind: str, candidates: List[str], profile: Any = None
) -> str:
    """扫码前先选游戏：一次只登一款，还是全登。"""
    title = "米游社" if kind == "mihoyo" else "森空岛"
    lines = [f"扫码登录{title}，先选登哪款："]
    for index, game in enumerate(candidates, start=1):
        lines.append(f"{index} {GAME_LABELS.get(game, game)}")
    lines.append(f"{len(candidates) + 1} 全部（{len(candidates)} 个）")
    lines.append("")
    lines.append(f"发 /日常 登录{title} 2")
    lines.append("一个游戏可以绑多个号：换个手机号再扫一次就多一个。")

    entries = list(getattr(profile, "entries", []) or []) if profile is not None else []
    mine = [item for item in entries if item.game in candidates]
    if mine:
        lines.append("")
        lines.append("已绑定：" + "、".join(item.name for item in mine))
    return "\n".join(lines)


def cooldown_text(seconds: int) -> str:
    return f"刚查过，{max(1, int(seconds))} 秒后再试。"


def cache_note(age_seconds: int) -> str:
    age = max(0, int(age_seconds))
    when = "刚刚" if age < 5 else f"{age} 秒前"
    return f"\n（{when}的缓存）"


def unbind_text(removed: List[str], remain: int) -> str:
    if not removed:
        return "没有找到要解绑的账号。"
    text = "已解绑：" + "、".join(removed)
    if remain:
        return text + f"\n还剩 {remain} 个。"
    return text + "\n已清空。"


def claim_text(added: int) -> str:
    if added <= 0:
        return "没有可认领的旧账号。"
    return f"已并入 {added} 个旧账号，配置已清空。"


def _truncate(text: str, max_lines: int = MAX_REPORT_LINES) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    kept = lines[:max_lines]
    kept.append(f"…（还有 {len(lines) - max_lines} 行，详见日志）")
    return "\n".join(kept)


def _who(item: Any) -> str:
    name = str(getattr(item, "game_name", "") or getattr(item, "game_id", ""))
    nick = str(getattr(item, "nickname", "") or "")
    return f"{name}（{nick}）" if nick else name


def _status_lines(item: Any) -> List[str]:
    """一个游戏一段，只留「做没做完 + 剩什么」。"""
    who = _who(item)
    if not getattr(item, "ok", True):
        reason = str(getattr(item, "error", "") or "查询失败")
        return [f"{who} 查询失败（{reason}）"]
    if getattr(item, "no_account", None):
        return [f"{who} 未创建角色"]
    if getattr(item, "stale", None):
        return [f"{who} 数据待更新"]

    counted = list(getattr(item, "counted_tasks", []) or [])
    if not counted:
        return [f"{who} 无判定项"]

    done = int(getattr(item, "done_count", 0) or 0)
    total = len(counted)
    head = f"{who} {done}/{total}"
    lines = [head + (" ✓" if getattr(item, "finished", False) else "")]
    pending = [task.describe() for task in getattr(item, "unfinished_tasks", []) or []]
    if pending:
        lines.append("  剩：" + "、".join(pending))
    info = [
        task.describe().replace("(", "（").replace(")", "）")
        for task in getattr(item, "info_tasks", []) or []
    ]
    if info:
        lines.append("  " + "、".join(info))
    return lines


def _rank(item: Any) -> int:
    """排序：没做完的先说，做完的垫底。"""
    if not getattr(item, "ok", True):
        return 2
    if getattr(item, "stale", None) or getattr(item, "no_account", None):
        return 3
    if getattr(item, "finished", False):
        return 4
    return 0 if getattr(item, "counted_tasks", None) else 1


def report_text(report: Any, *, title: str = "") -> str:
    """把 DailyReport 渲染成聊天可读的报告（不用表格线与状态码）。"""
    lines: List[str] = []
    if title:
        lines.append(title)
    stamp = getattr(report, "generated_at", None)
    clock = f"{stamp:%m-%d %H:%M}" if stamp else ""
    lines.append(clock if title else (f"日常 {clock}" if clock else "日常"))

    statuses = list(getattr(report, "statuses", []) or [])
    if not statuses:
        lines.append("没有可检查的账号。")
        return "\n".join(lines)

    for item in sorted(statuses, key=_rank):
        lines.append("")
        lines.extend(_status_lines(item))

    alerts = list(getattr(report, "resource_alerts", []) or [])
    activities = list(getattr(report, "activity_alerts", []) or [])
    if alerts or activities:
        lines.append("")
        for alert in alerts:
            lines.append(
                f"⚠️ {alert.game_name}{alert.resource} "
                f"{alert.current}/{alert.maximum}，快满了"
            )
        for alert in activities:
            days = alert.days_left
            left = f"还剩 {days} 天" if days >= 1 else "今天结束"
            lines.append(f"📅 {alert.game_name}「{alert.title}」{left}")

    return _truncate("\n".join(lines))


def push_text(report: Any) -> str:
    """推送文案：精简版报告（手机上不刷屏）。"""
    return report_text(report)



def weekly_text(digest: Dict[str, Any]) -> str:
    games = digest.get("games") or []
    days_count = int(digest.get("days_count") or 0)
    if not games:
        return "📊 近一周还没有巡检记录，先发一次 /日常。"

    lines = [f"📊 近 {days_count} 天完成情况"]
    for item in games[:12]:
        known = int(item.get("known_days") or 0)
        done = int(item.get("done_days") or 0)
        rate = (done / known * 100) if known else 0
        lines.append(
            f"{item.get('name') or item.get('game')} {done}/{known}（{rate:.0f}%）"
        )
    missed = [
        item for item in games if int(item.get("missed_days") or 0) > 0
    ]
    if missed:
        worst = max(missed, key=lambda item: int(item.get("missed_days") or 0))
        lines.append("")
        lines.append(
            f"漏得最多：{worst.get('name')}，{worst.get('missed_days')} 天"
        )
    return "\n".join(lines)


def activity_text(settings: PluginSettings, limit: int = 12) -> str:
    """活动列表：进行中按结束时间排序，未开启的单独一句带过。"""
    if not settings.activity_alert_enabled:
        return "活动提醒已关闭。"
    try:
        activities.ensure_fresh()
        items = activities.ongoing()
    except Exception as exc:  # noqa: BLE001
        return f"读取活动日历失败：{exc}"

    enabled = set(settings.game_ids)
    items = [item for item in items if item.game_id in enabled] if enabled else items
    if not items:
        return "暂无进行中的活动。"

    ongoing = [item for item in items if not item.not_started]
    upcoming = [item for item in items if item.not_started]
    lines = ["📅 活动日历"]
    for item in ongoing[:limit]:
        extra = item.countdown_text()
        if item.progress:
            extra = f"{extra}，{item.progress}"
        lines.append(f"{item.game_name} {item.title}")
        lines.append(f"  {extra}")
    if len(ongoing) > limit:
        lines.append(f"…还有 {len(ongoing) - limit} 个")
    if upcoming:
        lines.append("")
        lines.append(
            "未开启：" + "、".join(f"{item.game_name} {item.title}" for item in upcoming[:5])
        )
    return "\n".join(lines)


def login_done_text(kind: str, result: Any, extra: str = "") -> str:
    """登录回执：新增了哪个号、刷新了哪个号、这个号下没有哪款游戏。"""
    def name(item: Dict[str, str]) -> str:
        who = str(item.get("name") or "")
        label = str(item.get("label") or item.get("game") or "")
        return f"{label}（{who}）" if who else label

    added = [name(item) for item in getattr(result, "added", []) or []]
    updated = [name(item) for item in getattr(result, "updated", []) or []]
    missing = [name(item) for item in getattr(result, "missing", []) or []]
    touched = added + updated

    if len(touched) == 1 and not missing:
        return f"✅ 登录成功：{touched[0]}"

    lines = ["✅ 登录成功" if touched else "⚠️ 登录成功，但没有写入账号"]
    if added:
        lines.append("新增：" + "、".join(added))
    if updated:
        lines.append("刷新：" + "、".join(updated))
    if missing:
        lines.append("这个号下没有：" + "、".join(missing))
    return "\n".join(lines)


def alias_text(name: str, label: str) -> str:
    if not label:
        return f"已清除别名：{name}"
    return f"已改名：{name}"


def cred_alert_text(alerts: List[Dict[str, Any]]) -> str:
    names = "、".join(
        GAME_LABELS.get(str(item.get("game_id") or ""), str(item.get("game_id") or ""))
        for item in alerts
    )
    return f"🔑 凭证失效：{names}\n重新扫码：/日常 登录米游社 或 /日常 登录森空岛"


__all__ = [
    "account_lines",
    "activity_text",
    "alias_text",
    "binding_hint",
    "cache_note",
    "claim_text",
    "cooldown_text",
    "cred_alert_text",
    "help_text",
    "login_done_text",
    "login_menu_text",
    "push_text",
    "report_text",
    "status_text",
    "unbind_text",
    "weekly_text",
]
