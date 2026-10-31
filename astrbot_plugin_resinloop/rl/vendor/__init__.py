"""ResinLoop 引擎（原样移植自 https://github.com/R3vonutto/ResinLoop ，MIT）。

分层与原仓库完全一致：
    models    数据模型（TaskItem / GameStatus / ResourceAlert / PushResult）
    config    强类型配置对象与各游戏常量
    mihoyo_api / skland_api   HTTP 层（DS 签名、设备指纹、森空岛 HMAC）
    querier   查询层（按 provider 注册，支持 mock / miyoushe / skland）
    analyzer  判断层（汇总报告、渲染控制台与推送文案）
    activities / history / cred_health / push_guard  活动提醒、历史、凭证健康、推送回压
    *_login   米游社 / 鹰角通行证扫码登录

与上游的差异只有两类（为适配 AstrBot 运行环境）：
    1. 运行时状态目录统一由 .paths 解析到 AstrBot 插件数据目录；
    2. 模块间绝对导入改为相对导入，活动日历的账号来源由插件注入。
业务逻辑一行未改。
"""
