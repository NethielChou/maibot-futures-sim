"""配置模型：麦麦期货模拟盘（G2 骨架，字段与 G1 v0.2 对齐）。

约定：
- 所有字段必须有默认值（宿主 Runner 首次加载时以此生成 config.toml）。
- 不使用 None 当哨兵；TOML 无 null。
- 结构变更时递增 ``CONFIG_VERSION``。
"""

from maibot_sdk import Field, PluginConfigBase

CONFIG_VERSION = "0.8.3"


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件（宿主保留键）")
    config_version: str = Field(default=CONFIG_VERSION, description="配置版本；结构变更时递增")


class AccountConfig(PluginConfigBase):
    """虚拟账户设置。"""

    __ui_label__ = "账户"
    __ui_icon__ = "wallet"
    __ui_order__ = 10

    initial_balance: float = Field(default=1_000_000.0, ge=0.0, description="虚拟起始资金")
    slippage_ticks: int = Field(default=1, ge=0, le=20, description="模拟成交在对手价上的额外滑点（跳）")
    allow_short: bool = Field(default=True, description="是否允许做空")
    settle_enabled: bool = Field(default=True, description="是否启用每日结算（按交易所结算价）")
    settle_time: str = Field(default="15:10", description="每日结算时点（HH:MM，收盘后）")
    settle_use_last: bool = Field(default=False, description="结算价缺失时是否用最新价近似（国际连续行情）")
    settle_retry_seconds: int = Field(default=300, ge=30, le=3600, description="结算价缺失/部分完成时的重试间隔（秒）")


class MarketConfig(PluginConfigBase):
    """行情与合约池设置。"""

    __ui_label__ = "行情"
    __ui_icon__ = "activity"
    __ui_order__ = 20

    source: str = Field(default="sina", description="行情源；首版仅 sina")
    markets: list[str] = Field(
        default_factory=lambda: ["cn"],
        description="交易市场：cn=国内商品期货，intl=国际期货（可同时启用）",
    )
    symbols: list[str] = Field(
        default_factory=list,
        description="高级覆盖：留空按市场自动选择品种池；一般不用填",
    )
    quote_poll_seconds: int = Field(default=60, ge=10, le=600, description="行情采样间隔（秒）")
    quote_timeout_seconds: int = Field(default=8, ge=1, le=30, description="单次行情请求超时（秒）")
    stale_quote_seconds: int = Field(default=180, ge=10, le=1800, description="行情时间戳超过该秒数视为过期")
    sessions: list[str] = Field(
        default_factory=lambda: ["09:00-10:15", "10:30-11:30", "13:30-15:00"],
        description="日盘交易时段（左闭右开）；夜盘按品种单独配置",
    )
    night_enabled: bool = Field(
        default=True,
        description="启用夜盘：RB/M/I/SR/CF 至 23:00，CU/AL 至 01:00，SC/AU/AG 至 02:30",
    )


class RiskConfig(PluginConfigBase):
    """风控限额。"""

    __ui_label__ = "风控"
    __ui_icon__ = "shield"
    __ui_order__ = 30

    max_total_margin_ratio: float = Field(default=0.30, ge=0.05, le=1.0, description="总保证金占用上限（占权益比例）")
    max_positions: int = Field(default=3, ge=1, le=10, description="最大同时持仓合约数")
    max_lots_per_symbol: int = Field(default=1, ge=1, le=20, description="单合约最大手数（首版不加仓）")
    daily_trade_limit: int = Field(default=6, ge=0, le=50, description="每日开仓笔数上限")
    daily_loss_limit_ratio: float = Field(default=0.03, ge=0.0, le=0.2, description="当日亏损达到权益比例后停手")
    stop_loss_atr_mult: float = Field(default=2.0, ge=0.5, le=10.0, description="止损：ATR 倍数")
    take_profit_atr_mult: float = Field(default=3.0, ge=0.5, le=20.0, description="止盈：ATR 倍数")
    cooldown_minutes_per_symbol: int = Field(default=30, ge=0, le=240, description="同合约开仓冷却（分钟）")
    max_drift_ticks: int = Field(default=5, ge=1, le=100, description="决策后重新取价时允许的最大不利漂移（跳）")
    fill_max_quote_age_seconds: int = Field(default=10, ge=1, le=60, description="成交前行情最大允许年龄（秒）")
    max_abs_price_change_ratio: float = Field(default=0.10, ge=0.01, le=0.5, description="单次采样异常涨跌幅阈值")
    expiry_block_days: int = Field(default=3, ge=0, le=10, description="到期前多少个交易日停止开新仓")


class DecisionConfig(PluginConfigBase):
    """决策与 LLM 设置。"""

    __ui_label__ = "决策"
    __ui_icon__ = "brain"
    __ui_order__ = 40

    auto_trade: bool = Field(default=False, description="是否自动交易；默认关闭，部署并检查后再打开")
    llm_enabled: bool = Field(default=True, description="是否使用 LLM 决策（关闭=纯规则/不交易，取决于模式）")
    interval_minutes: int = Field(default=30, ge=1, le=240, description="决策间隔（分钟）")
    min_interval_minutes: int = Field(default=10, ge=1, le=240, description="决策间隔下限")
    max_interval_minutes: int = Field(default=90, ge=1, le=240, description="决策间隔上限")
    warmup_samples: int = Field(default=20, ge=5, le=240, description="规则预热所需采样数")
    max_llm_calls_per_day: int = Field(default=30, ge=0, le=500, description="每日 LLM 调用上限（含重试与播报）")
    prompt_char_limit: int = Field(default=8000, ge=1000, le=30000, description="决策 prompt 字符上限")
    decision_max_tokens: int = Field(default=1200, ge=100, le=8000, description="决策输出 token 上限")
    report_max_tokens: int = Field(default=300, ge=50, le=2000, description="播报输出 token 上限")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0, description="决策温度")
    report_temperature: float = Field(default=0.9, ge=0.0, le=2.0, description="播报温度")
    decision_task_name: str = Field(default="planner", description="宿主决策模型任务名")
    report_task_name: str = Field(default="replyer", description="宿主播报模型任务名")
    request_timeout_ms: int = Field(default=120000, ge=5000, le=600000, description="单次 LLM 调用超时（毫秒）")
    recent_trades_in_context: int = Field(default=5, ge=0, le=20, description="决策上下文携带的最近交易条数")


class NotifyConfig(PluginConfigBase):
    """播报设置。"""

    __ui_label__ = "播报"
    __ui_icon__ = "megaphone"
    __ui_order__ = 50

    platform: str = Field(default="qq", description="播报平台")
    account_id: str = Field(default="", description="限定账号 ID；留空不限制")
    group_ids: list[str] = Field(default_factory=list, description="播报目标群号列表")
    private_user_ids: list[str] = Field(default_factory=list, description="私聊播报用户 ID 列表")
    private_enabled: bool = Field(default=False, description="是否启用私聊播报")
    event_reports: bool = Field(default=True, description="开平仓是否即时播报")
    close_report_enabled: bool = Field(default=True, description="是否发送收盘总结")
    close_report_delay_minutes: int = Field(default=5, ge=0, le=60, description="收盘后延迟多少分钟发总结")
    merge_messages: bool = Field(
        default=True,
        description="是否把插件消息合并成一条转发消息发送（防刷屏；失败自动回退普通文本）",
    )
    merge_window_ms: int = Field(
        default=800, ge=100, le=5000, description="同一条命令/事件的多段消息合并等待窗口（毫秒）"
    )


class AdminConfig(PluginConfigBase):
    """管理员白名单（0.2 起唯一管理角色）。"""

    __ui_label__ = "管理员"
    __ui_icon__ = "users"
    __ui_order__ = 61

    users: list[str] = Field(
        default_factory=list,
        description="管理员白名单，格式 platform:user_id，如 qq:123456；留空=没有管理员",
    )


class InfluenceConfig(PluginConfigBase):
    """影响源开关（群聊 / 新闻研报）；全部默认关闭。"""

    __ui_label__ = "影响源"
    __ui_icon__ = "sparkles"
    __ui_order__ = 62

    enabled: bool = Field(default=False, description="影响源总开关")
    group_enabled: bool = Field(default=False, description="是否读取群聊作为决策参考（只读宿主已有记录，不落盘）")
    group_window_minutes: int = Field(default=20, ge=1, le=240, description="群聊参考窗口（分钟）")
    group_max_messages: int = Field(default=30, ge=5, le=200, description="群聊最多读取条数")
    news_enabled: bool = Field(default=False, description="是否抓取新闻/研报标题作为决策参考（内存缓存）")
    news_interval_minutes: int = Field(default=45, ge=5, le=360, description="新闻抓取最小间隔（分钟）")
    news_max_items: int = Field(default=5, ge=1, le=20, description="每次注入的最大新闻条数")


class StorageConfig(PluginConfigBase):
    """存储设置。"""

    __ui_label__ = "存储"
    __ui_icon__ = "database"
    __ui_order__ = 70

    history_limit: int = Field(default=200, ge=20, le=5000, description="决策/交易历史保留条数")


class OfficialDataConfig(PluginConfigBase):
    """交易所官方数据同步设置。"""

    __ui_label__ = "官方数据"
    __ui_icon__ = "database"
    __ui_order__ = 65

    enabled: bool = Field(default=True, description="是否自动同步交易所日历/结算数据")
    calendar_sync_hours: int = Field(
        default=6, ge=1, le=72, description="交易日历自动同步间隔（小时）"
    )
    settlement_enabled: bool = Field(
        default=True, description="结算时优先使用交易所官方结算价"
    )
    timeout_seconds: int = Field(default=15, ge=3, le=60, description="交易所请求超时（秒）")
    cache_enabled: bool = Field(default=True, description="是否缓存最近一次官方数据（插件本地）")


class MaiFuturesConfig(PluginConfigBase):
    """麦麦期货模拟盘总配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    account: AccountConfig = Field(default_factory=AccountConfig)
    market: MarketConfig = Field(default_factory=MarketConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    decision: DecisionConfig = Field(default_factory=DecisionConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    influence: InfluenceConfig = Field(default_factory=InfluenceConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    official: OfficialDataConfig = Field(default_factory=OfficialDataConfig)
