# ResinLoop · 日常巡查

把 [ResinLoop](https://github.com/R3vonutto/ResinLoop) 的引擎搬进 AstrBot：到点查一轮游戏日常，没做完就发消息。

**只读**：不签到、不领奖励、不改任何游戏数据。

支持原神、崩坏：星穹铁道、绝区零（米游社），明日方舟、终末地（森空岛）。

## 装

1. 插件管理 → 安装插件 → 选 `astrbot_plugin_resinloop.zip`
2. 私聊机器人：

```
/日常 登录米游社     # 原神 / 崩铁 / 绝区零
/日常 登录森空岛     # 明日方舟 / 终末地
```

扫码后自动回填 UID、区服、昵称。

3. 要收提醒的会话里发 `/日常 订阅`。检查时间在插件配置的「检查时间点」里改。

## 指令

| 指令 | 作用 |
| --- | --- |
| `/日常` | 查我的账号 |
| `/日常 状态` | 绑定与提醒 |
| `/日常 周报` | 近 7 天完成情况 |
| `/日常 活动` | 即将结束的活动 |
| `/日常 登录米游社` | 扫码绑原神 / 崩铁 / 绝区零 |
| `/日常 登录森空岛` | 扫码绑明日方舟 / 终末地 |
| `/日常 订阅` / `退订` | 本会话接收 / 取消提醒 |
| `/日常 解绑 原神` | 删绑定（`全部` 清空） |
| `/日常 测试推送` | 按当前结果推一条 |

别名：`/resin`、`/树脂`、`/日常巡查`。

也可以直接问 AI「我今天的日常做完了吗」——插件注册了 LLM 工具 `query_game_daily_status`。

## 多人

账号按「平台 + 发送者」存，每人一份。群里各查各的，互不覆盖。

提醒默认发私聊（`push_scope=private_first`），群里不会出现别人的账号状态。改 `session_only` 则发订阅会话，改 `private_only` 只发私聊。

每个人自己扫码绑定，不需要管理员。

## 配置

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | 开 | 关掉后只有手动 `/日常` 才查 |
| `check_times` | `08:30`,`20:30` | 每天检查的时间点 |
| `catch_up_minutes` | 180 | 错过时间点的补发窗口，0 = 不补 |
| `always_notify` | 关 | 全部完成也发一条 |
| `test_mode` | `auto` | `force_alert` 每轮都推 / `force_silent` 只写日志 |
| `push_scope` | `private_first` | 提醒发到哪 |
| `resource_thresholds` | 见下 | 体力预警阈值，填 0 关闭该游戏 |
| `activity_alert_days` | 3 | 活动结束前几天提醒 |
| `mihoyo_enabled` | 开 | 风控严重时可只查森空岛系 |
| `proxy` | 空 | 米游社请求代理 |

默认阈值：原神 160、崩铁 240、绝区零 200、方舟 135、终末地 300。当前值 ≥ 阈值就推一条。

其他：`query_cache_seconds`（90）、`command_cooldown_seconds`（10）、`max_login_sessions`（3）、`webhook_enabled` / `webhooks`（Bark、Server酱、飞书等）。

`accounts` 与 `push_targets` 是旧版全局数据，现在只用于 `/日常 认领` 迁移，不用手填。

## 目录

```
main.py                 命令与生命周期
_conf_schema.json       配置表单
rl/settings.py          配置 → 引擎参数
rl/userstore.py         每人的账号与订阅
rl/user_history.py      每人的历史存档
rl/runner.py            查询与判断
rl/pusher.py            会话推送 + Webhook
rl/scheduler.py         定时检查（含补发）
rl/login.py             扫码登录
rl/texts.py             回复文案
rl/vendor/              上游 core/ 移植
tests/selftest.py       离线自测
```

运行时数据在 `data/plugin_data/astrbot_plugin_resinloop/`：`users.json`（账号，明文，别外发）、`history/<用户>.json`、`logs/device_identity.json`。

## 自测

```bash
python tests/selftest.py
```

不装 AstrBot 也能跑，110 项断言，覆盖配置解析、命令分派、调度补发、报告渲染、凭证回写、多人隔离。

## 与上游的差异

引擎（`rl/vendor/`，8.7k 行）原样移植，只改了数据目录、导入方式、活动日历的账号来源。

换掉的是外壳：

| 上游桌面版 | 本插件 |
| --- | --- |
| 守护进程 + 托盘 | 插件内轮询 + 补发窗口 |
| WebView2 控制台 | AstrBot 配置表单 |
| 终端 ASCII 二维码 | 二维码图片发到聊天 |
| 手填 config.json | 自动写回本人的账号表 |
| Bark 等 5 个通道 | 机器人会话 + Webhook |

## 常见问题

**收不到提醒**：先看 `/日常 状态` 里的「订阅会话」是不是 0；再看检查时间点是否配了；`test_mode` 是不是 `force_silent`。

**查询失败 / 5003**：米游社风控。先重新扫码（会顺带绑定设备指纹），仍失败可设 `proxy`，或临时把 `mihoyo_enabled` 关掉只查森空岛。

**群友能看到我的账号吗**：看不到。各存各的，提醒默认走私聊。

**升级后旧账号不见了**：还在插件配置的 `accounts` 里，管理员发 `/日常 认领` 并入自己名下。

## 许可

沿用上游 MIT，见 [LICENSE](LICENSE)。
