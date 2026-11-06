"""WebUI 配置页中文化与分组。

只重组 SDK 生成的配置 Schema（页签/分组/字段标题/提示），不改配置模型、
默认值和 TOML 路径。宿主设置页如果拿不到本文件，字段标题会退回英文键名。
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class _UiSection:
    """配置页里的一个展示块。"""

    source: str
    title: str
    description: str
    fields: dict[str, str]
    collapsed: bool = False


_UI_SECTIONS: dict[str, _UiSection] = {
    "account": _UiSection(
        source="account",
        title="虚拟账户",
        description="麦麦币模拟账户的基础规则；新手保持默认即可。",
        fields={
            "initial_balance": "起始资金（麦麦币）",
            "slippage_ticks": "模拟滑点（跳）",
            "allow_short": "允许做空",
            "settle_enabled": "每日结算",
            "settle_time": "结算时间",
            "settle_use_last": "缺结算价时用最新价",
            "settle_retry_seconds": "结算重试间隔（秒）",
        },
    ),
    "market_core": _UiSection(
        source="market",
        title="市场与品种",
        description="先选市场，再决定是否开户/参与夜盘；默认使用插件内置的代表品种。",
        fields={
            "markets": "交易市场",
            "symbols": "品种范围（高级覆盖）",
            "night_enabled": "启用夜盘",
        },
    ),
    "market_advanced": _UiSection(
        source="market",
        title="行情采集（高级）",
        description="一般不需要改；只有网络慢或行情异常时才调整。",
        fields={
            "source": "行情源",
            "quote_poll_seconds": "行情刷新间隔（秒）",
            "quote_timeout_seconds": "行情请求超时（秒）",
            "stale_quote_seconds": "行情过期判定（秒）",
            "sessions": "日盘交易时段",
        },
        collapsed=True,
    ),
    "risk_core": _UiSection(
        source="risk",
        title="风控（常用）",
        description="程序风控优先于麦麦的判断；这些上限决定它最多能冒险到什么程度。",
        fields={
            "max_total_margin_ratio": "总保证金上限（权益比例）",
            "max_positions": "最多同时持仓",
            "daily_trade_limit": "每日开仓上限（笔）",
            "daily_loss_limit_ratio": "当日亏损停手线",
            "stop_loss_atr_mult": "止损（ATR 倍数）",
            "take_profit_atr_mult": "止盈（ATR 倍数）",
        },
    ),
    "risk_advanced": _UiSection(
        source="risk",
        title="风控（高级）",
        description="成交滑点、冷却、异常行情过滤和到期保护；不懂就保持默认。",
        fields={
            "max_lots_per_symbol": "单品种最大手数",
            "cooldown_minutes_per_symbol": "同品种开仓冷却（分钟）",
            "max_drift_ticks": "决策后允许滑价（跳）",
            "fill_max_quote_age_seconds": "成交前行情最大年龄（秒）",
            "max_abs_price_change_ratio": "异常涨跌幅过滤阈值",
            "expiry_block_days": "到期前停止开仓（交易日）",
        },
        collapsed=True,
    ),
    "decision_core": _UiSection(
        source="decision",
        title="麦麦决策（常用）",
        description="麦麦多久看一次盘、每天最多用多少次模型；自动交易默认关闭。",
        fields={
            "auto_trade": "自动交易",
            "llm_enabled": "启用 LLM 决策",
            "interval_minutes": "决策间隔（分钟）",
            "max_llm_calls_per_day": "每日 LLM 调用上限",
            "warmup_samples": "规则预热采样数",
        },
    ),
    "decision_advanced": _UiSection(
        source="decision",
        title="模型与节奏（高级）",
        description="模型任务名、温度、输出长度和超时；改错会导致决策失败。",
        fields={
            "min_interval_minutes": "决策间隔下限（分钟）",
            "max_interval_minutes": "决策间隔上限（分钟）",
            "prompt_char_limit": "决策 prompt 字符上限",
            "decision_max_tokens": "决策输出 token 上限",
            "report_max_tokens": "播报输出 token 上限",
            "temperature": "决策温度",
            "report_temperature": "播报温度",
            "decision_task_name": "决策模型任务名",
            "report_task_name": "播报模型任务名",
            "request_timeout_ms": "单次 LLM 超时（毫秒）",
            "recent_trades_in_context": "上下文携带最近成交条数",
        },
        collapsed=True,
    ),
    "notify": _UiSection(
        source="notify",
        title="播报与群聊",
        description="麦麦把交易汇报发到哪里，以及是否合并成一条转发消息防刷屏。",
        fields={
            "platform": "播报平台",
            "group_ids": "播报群号",
            "private_user_ids": "私聊用户 ID",
            "private_enabled": "启用私聊播报",
            "event_reports": "开平仓即时播报",
            "close_report_enabled": "每日结算播报",
            "merge_messages": "合并转发防刷屏",
            "merge_window_ms": "合并等待窗口（毫秒）",
        },
    ),
    "admin": _UiSection(
        source="admin",
        title="管理员",
        description="只有名单里的账号能使用试跑、暂停、手动买卖和平仓等控制命令；普通群友只能查询。",
        fields={
            "users": "管理员账号",
        },
    ),
    "influence": _UiSection(
        source="influence",
        title="影响源（可选）",
        description="让麦麦参考群聊和新闻标题；默认全关，开启后只读、不把摘要写入插件账本。",
        fields={
            "enabled": "影响源总开关",
            "group_enabled": "参考群聊",
            "group_window_minutes": "群聊参考窗口（分钟）",
            "group_max_messages": "群聊最多读取条数",
            "news_enabled": "参考新闻/研报",
            "news_interval_minutes": "新闻抓取间隔（分钟）",
            "news_max_items": "每次注入新闻条数",
        },
        collapsed=True,
    ),
    "official": _UiSection(
        source="official",
        title="交易所官方数据",
        description="自动同步休市安排和结算价；源不可用时自动回退到行情/静态日历。",
        fields={
            "enabled": "启用官方数据同步",
            "calendar_sync_hours": "日历同步间隔（小时）",
            "settlement_enabled": "优先使用官方结算价",
            "timeout_seconds": "官方请求超时（秒）",
            "cache_enabled": "缓存最近官方数据",
        },
        collapsed=True,
    ),
    "plugin": _UiSection(
        source="plugin",
        title="插件信息",
        description="用于版本识别，保持默认即可。",
        fields={
            "enabled": "启用插件",
            "config_version": "配置版本",
        },
        collapsed=True,
    ),
}

_UI_TABS: dict[str, tuple[str, tuple[str, ...]]] = {
    "basic": ("常用", ("account", "market_core", "notify", "admin")),
    "trading": ("交易", ("decision_core", "risk_core", "influence")),
    "advanced": (
        "高级",
        (
            "market_advanced",
            "decision_advanced",
            "risk_advanced",
            "official",
            "plugin",
        ),
    ),
}

_UI_HINTS: dict[str, str] = {
    "account.initial_balance": "只对新建账户生效；已有账户要改起始资金，请用 /期货 重置 确认。",
    "account.slippage_ticks": "模拟成交在对手价上多加的跳数；1 跳更接近真实滑点，0 最理想化。",
    "account.allow_short": "关闭后麦麦只能做多；国内商品期货默认允许做空。",
    "account.settle_enabled": "每天收盘后按交易所结算价结算；关闭只影响结算播报/成本基准，不影响行情。",
    "account.settle_time": "国内默认 15:10；国际在插件内部单独按 05:10 处理。格式 HH:MM。",
    "account.settle_use_last": "国际连续行情没有昨结算时用最新价近似；国内一般保持关闭。",
    "account.settle_retry_seconds": "某个品种结算价缺失时，隔多久再试一次。",
    "market.markets": "填 cn=国内，intl=国际；两个都填会同时开两个独立账户。国内用户建议只保留 cn。",
    "market.symbols": "一般留空，插件会使用内置的 20 个国内代表品种 + 9 个国际品种。只有你要手动限制品种时才填品种名（如 螺纹钢、甲醇、布伦特原油）或合约代码（如 RB2701）。",
    "market.night_enabled": "国内启用夜盘：多数品种到 23:00，铜铝到 01:00，原油/贵金属到 02:30；生猪、鸡蛋、苹果仅日盘。",
    "market.source": "当前版本固定使用新浪期货行情，不要改。",
    "market.quote_poll_seconds": "多久拉一次行情；太小会增加网络请求，默认 60 秒。",
    "market.quote_timeout_seconds": "单次行情请求最多等多久；网络差可适当加大。",
    "market.stale_quote_seconds": "超过这个年龄的行情不参与决策；休市时不会每分钟刷日志。",
    "market.sessions": "日盘时段，格式 HH:MM-HH:MM；夜盘由品种自动附加，一般不要改。",
    "risk.max_total_margin_ratio": "全部持仓保证金 / 账户权益的上限；默认 30%，超过就拒绝开新仓。",
    "risk.max_positions": "最多同时持有几个合约；不是手数。",
    "risk.daily_trade_limit": "每天最多开几笔；平仓和结算不算在内。",
    "risk.daily_loss_limit_ratio": "当日权益从起始权益回撤达到比例后停手；默认 3%。",
    "risk.stop_loss_atr_mult": "用 ATR 计算止损距离；数值越大，止损越宽。",
    "risk.take_profit_atr_mult": "用 ATR 计算止盈距离；数值越大，越晚止盈。",
    "risk.max_lots_per_symbol": "单品种最多几手；当前版本默认不允许加仓。",
    "risk.cooldown_minutes_per_symbol": "同一品种两次开仓之间至少隔多久。",
    "risk.max_drift_ticks": "麦麦决定后重新取价，价格向不利方向漂移超过该跳数就放弃。",
    "risk.fill_max_quote_age_seconds": "成交前行情太旧就不成交，避免用过期价格撮合。",
    "risk.max_abs_price_change_ratio": "单次采样涨跌幅超过阈值视为异常行情；扩大板/跳空时可能触发。",
    "risk.expiry_block_days": "合约最后交易日前几天停止开新仓；到期强平不受此限制。",
    "decision.auto_trade": "总开关，默认关闭。建议先在群里用 /期货 状态、/期货 试跑 验证，再打开。",
    "decision.llm_enabled": "关闭后不调用模型，也不会自动开新仓；风控/结算/播报仍然运行。",
    "decision.interval_minutes": "麦麦每隔多久做一次新的决策；不是行情刷新间隔。",
    "decision.max_llm_calls_per_day": "每天模型调用预算，包含失败重试；用完后当天不再自动决策。",
    "decision.warmup_samples": "启动后先积累多少条行情样本才允许开仓；用于计算 ATR。",
    "decision.min_interval_minutes": "决策节奏下限；一般不用改。",
    "decision.max_interval_minutes": "决策节奏上限；一般不用改。",
    "decision.prompt_char_limit": "一次决策 prompt 的最大字符数；太小会截断上下文。",
    "decision.decision_max_tokens": "模型输出交易 JSON 的 token 上限。",
    "decision.report_max_tokens": "模型生成播报文字的 token 上限。",
    "decision.temperature": "决策温度；越低越稳定，越高越随机。",
    "decision.report_temperature": "播报温度；只影响说话风格，不影响交易。",
    "decision.decision_task_name": "宿主模型任务名，默认 planner；不懂不要改。",
    "decision.report_task_name": "宿主模型任务名，默认 replyer；不懂不要改。",
    "decision.request_timeout_ms": "单次模型调用最长等待；命令超时已按它留足余量。",
    "decision.recent_trades_in_context": "决策时给模型看最近多少条成交，帮助它复盘。",
    "notify.platform": "目前支持 qq；需和宿主适配器平台一致。",
    "notify.group_ids": "允许麦麦播报和响应命令的群号；一行一个。不填则不会主动播报。",
    "notify.private_user_ids": "允许私聊播报的用户 ID；一般留空。",
    "notify.private_enabled": "默认关闭；开启前确认私聊对象在名单里。",
    "notify.event_reports": "每次开仓/平仓是否立即播报；关闭后只剩结算/命令回复。",
    "notify.close_report_enabled": "每日结算后是否发总结。",
    "notify.merge_messages": "把多段消息合成一张转发卡片，防止刷屏；合并失败会自动回退普通文本。",
    "notify.merge_window_ms": "同一次命令/事件的多段消息等待多久再合并；默认 800 毫秒。",
    "admin.users": "管理员格式 platform:user_id，例如 qq:123456；一行一个。留空=没有管理员，控制命令全部不可用。",
    "influence.enabled": "总开关；关掉后下面所有影响源都不生效。",
    "influence.group_enabled": "让麦麦参考目标群最近的聊天；只读内存摘要，不写入账本。",
    "influence.group_window_minutes": "回看群里最近多少分钟的聊天。",
    "influence.group_max_messages": "最多读取多少条群聊消息作为参考。",
    "influence.news_enabled": "抓取免费新闻/研报标题作为参考；不保证时效和完整性。",
    "influence.news_interval_minutes": "两次新闻抓取至少间隔多久；避免频繁请求。",
    "influence.news_max_items": "每次最多把几条新闻标题放进决策上下文。",
    "official.enabled": "自动同步交易所休市安排/结算价；失败时回退，不影响交易主流程。",
    "official.calendar_sync_hours": "多久重新拉一次交易所日历；默认 6 小时。",
    "official.settlement_enabled": "每日结算优先使用交易所官方结算价，缺失再用行情。",
    "official.timeout_seconds": "单次交易所请求超时。",
    "official.cache_enabled": "把最近一次官方数据缓存在插件自己的 data 目录；重启后可用。",
    "plugin.enabled": "插件的总开关（宿主保留键）。",
    "plugin.config_version": "由插件维护，不要手动改。",
}

# 这些字段当前版本没有实际使用或只应由开发者维护，设置页不展示/不允许改
_UI_HIDDEN: set[str] = {
    "notify.account_id",
    "notify.close_report_delay_minutes",
    "storage.history_limit",
}
_UI_DISABLED: set[str] = {
    "plugin.config_version",
    "market.source",
}


def build_config_ui_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """在 SDK 生成的 Schema 上补中文页签、分组、字段标题和提示。"""

    source_sections = schema.get("sections") or {}
    sections: dict[str, Any] = {}
    tabs: list[dict[str, Any]] = []
    for tab_id, (tab_title, section_ids) in _UI_TABS.items():
        tabs.append({"id": tab_id, "title": tab_title, "sections": list(section_ids)})
        for section_id in section_ids:
            spec = _UI_SECTIONS[section_id]
            template = source_sections.get(spec.source)
            if template is None:
                continue
            template_fields = template.get("fields") or {}
            fields: dict[str, Any] = {}
            for order, (field_name, label) in enumerate(spec.fields.items()):
                field_template = template_fields.get(field_name)
                if field_template is None:
                    continue
                path = f"{spec.source}.{field_name}"
                entry = dict(field_template)
                hint = _UI_HINTS.get(path, entry.get("description") or "")
                entry.update(
                    label=label,
                    hint=hint,
                    description=hint,
                    order=order,
                )
                if path in _UI_HIDDEN:
                    entry["hidden"] = True
                if path in _UI_DISABLED:
                    entry["disabled"] = True
                fields[field_name] = entry
            if not fields:
                continue
            sections[section_id] = {
                **template,
                "name": spec.source,
                "title": spec.title,
                "description": spec.description,
                "collapsed": spec.collapsed,
                "order": len(sections),
                "fields": fields,
            }
    return {
        **schema,
        "sections": sections,
        "layout": {"type": "tabs", "tabs": tabs},
    }
