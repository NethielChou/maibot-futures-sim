"""合约参数表（首版 3 个商品期货合约）。

说明：
- 参数为**近似值**，用于模拟盘；不构成交易所精确参数。
- 首版不做自动换月：``last_trade_date`` 到期前 3 个交易日停止开新仓，最后交易日强平。
- ``last_trade_date`` 待按交易所规则逐项核对（G3 注释，G4 测试只做逻辑验证）。
"""

from dataclasses import dataclass, replace
from datetime import date, timedelta


class ContractError(ValueError):
    """合约不存在或参数非法。"""


@dataclass(frozen=True)
class ContractSpec:
    """单个期货合约的模拟参数。"""

    symbol: str
    name: str
    exchange: str
    multiplier: float
    tick_size: float
    margin_rate: float
    fee_per_lot: float
    fee_rate: float
    last_trade_date: str  # YYYY-MM-DD
    stop_take_enabled: bool = True
    session_profile: str = "day"
    rollover_to: str = ""
    rollover_days: int = 3
    display_name: str = ""  # 前台展示名：不带合约月份，如“螺纹钢”

    def notional(self, price: float, lots: int) -> float:
        """名义价值 = 价格 × 乘数 × 手数。"""
        return float(price) * self.multiplier * int(lots)

    def margin(self, price: float, lots: int) -> float:
        """交易保证金 = 名义价值 × 保证金率。"""
        return self.notional(price, lots) * self.margin_rate

    def fee(self, price: float, lots: int) -> float:
        """手续费 = 每手固定 + 成交额 × 费率（近似）。"""
        return self.fee_per_lot * int(lots) + self.notional(price, lots) * self.fee_rate

    def days_to_expiry(self, today: date) -> int:
        """距最后交易日的自然日数。"""
        last_day = date.fromisoformat(self.last_trade_date)
        return (last_day - today).days

    def trading_days_to_last(self, today: date) -> int:
        """距最后交易日的交易日数（按工作日近似，含最后交易日；不含节假日）。

        返回 0 表示最后交易日已过；返回 1 表示今天就是最后交易日。
        """

        last_day = date.fromisoformat(self.last_trade_date)
        if today > last_day:
            return 0
        remaining = 0
        current = today
        while current <= last_day:
            if current.weekday() < 5:  # 周一到周五
                remaining += 1
            current += timedelta(days=1)
        return remaining

    def is_expired(self, today: date) -> bool:
        """最后交易日已过（含当天收盘后视为过期）。"""
        return today > date.fromisoformat(self.last_trade_date)

    def last_trading_day_today(self, today: date) -> bool:
        """今天是否为最后交易日。"""
        return today == date.fromisoformat(self.last_trade_date)


DEFAULT_CONTRACTS: dict[str, ContractSpec] = {
    # 上期所螺纹钢：10 吨/手，最小变动 1 元/吨
    "RB2701": ContractSpec(
        symbol="RB2701",
        name="螺纹钢2701",
        exchange="SHFE",
        multiplier=10.0,
        tick_size=1.0,
        margin_rate=0.10,
        fee_per_lot=0.0,
        fee_rate=0.0001,
        last_trade_date="2027-01-15",
    ),
    # 大商所豆粕：10 吨/手，最小变动 1 元/吨
    "M2701": ContractSpec(
        symbol="M2701",
        name="豆粕2701",
        exchange="DCE",
        multiplier=10.0,
        tick_size=1.0,
        margin_rate=0.10,
        fee_per_lot=1.5,
        fee_rate=0.0,
        last_trade_date="2027-01-14",
    ),
    # 上期所沪金：1000 克/手，最小变动 0.02 元/克
    "AU2612": ContractSpec(
        symbol="AU2612",
        name="沪金2612",
        exchange="SHFE",
        multiplier=1000.0,
        tick_size=0.02,
        margin_rate=0.10,
        fee_per_lot=10.0,
        fee_rate=0.0,
        last_trade_date="2026-12-15",
    ),
    # 上期所沪银：15 千克/手，最小变动 1 元/千克
    "AG2612": ContractSpec(
        symbol="AG2612",
        name="沪银2612",
        exchange="SHFE",
        multiplier=15.0,
        tick_size=1.0,
        margin_rate=0.10,
        fee_per_lot=0.0,
        fee_rate=0.00005,
        last_trade_date="2026-12-15",
    ),
    # 上期所沪铜：5 吨/手，最小变动 10 元/吨
    "CU2612": ContractSpec(
        symbol="CU2612",
        name="沪铜2612",
        exchange="SHFE",
        multiplier=5.0,
        tick_size=10.0,
        margin_rate=0.10,
        fee_per_lot=0.0,
        fee_rate=0.00005,
        last_trade_date="2026-12-15",
    ),
    # 大商所铁矿石：100 吨/手，最小变动 0.5 元/吨
    "I2701": ContractSpec(
        symbol="I2701",
        name="铁矿石2701",
        exchange="DCE",
        multiplier=100.0,
        tick_size=0.5,
        margin_rate=0.10,
        fee_per_lot=0.0,
        fee_rate=0.0001,
        last_trade_date="2027-01-15",
    ),
    # 上期能源原油：1000 桶/手，最小变动 0.1 元/桶
    "SC2701": ContractSpec(
        symbol="SC2701",
        name="原油2701",
        exchange="INE",
        multiplier=1000.0,
        tick_size=0.1,
        margin_rate=0.10,
        fee_per_lot=20.0,
        fee_rate=0.0,
        last_trade_date="2026-12-31",
    ),
    # 上期所沪铝：5 吨/手，最小变动 5 元/吨
    "AL2612": ContractSpec(
        symbol="AL2612",
        name="沪铝2612",
        exchange="SHFE",
        multiplier=5.0,
        tick_size=5.0,
        margin_rate=0.10,
        fee_per_lot=3.0,
        fee_rate=0.0,
        last_trade_date="2026-12-15",
    ),
    # 郑商所白糖：10 吨/手，最小变动 1 元/吨
    "SR2701": ContractSpec(
        symbol="SR2701",
        name="白糖2701",
        exchange="CZCE",
        multiplier=10.0,
        tick_size=1.0,
        margin_rate=0.07,
        fee_per_lot=3.0,
        fee_rate=0.0,
        last_trade_date="2027-01-15",
    ),
    # 郑商所棉花：5 吨/手，最小变动 5 元/吨
    "CF2701": ContractSpec(
        symbol="CF2701",
        name="棉花2701",
        exchange="CZCE",
        multiplier=5.0,
        tick_size=5.0,
        margin_rate=0.07,
        fee_per_lot=4.3,
        fee_rate=0.0,
        last_trade_date="2027-01-15",
    ),
    # ---- 扩展品种：化工 / 养殖 / 建材 / 油脂 / 食品（参数为模拟近似值） ----
    "MA2701": ContractSpec(
        symbol="MA2701",
        name="甲醇2701",
        exchange="CZCE",
        multiplier=10.0,
        tick_size=1.0,
        margin_rate=0.08,
        fee_per_lot=2.5,
        fee_rate=0.0,
        last_trade_date="2027-01-15",
    ),
    "LH2701": ContractSpec(
        symbol="LH2701",
        name="生猪2701",
        exchange="DCE",
        multiplier=16.0,
        tick_size=5.0,
        margin_rate=0.08,
        fee_per_lot=2.5,
        fee_rate=0.0,
        last_trade_date="2027-01-15",
    ),
    "FG2701": ContractSpec(
        symbol="FG2701",
        name="玻璃2701",
        exchange="CZCE",
        multiplier=20.0,
        tick_size=1.0,
        margin_rate=0.10,
        fee_per_lot=6.0,
        fee_rate=0.0,
        last_trade_date="2027-01-15",
    ),
    "SA2701": ContractSpec(
        symbol="SA2701",
        name="纯碱2701",
        exchange="CZCE",
        multiplier=20.0,
        tick_size=1.0,
        margin_rate=0.10,
        fee_per_lot=3.5,
        fee_rate=0.0,
        last_trade_date="2027-01-15",
    ),
    "UR2701": ContractSpec(
        symbol="UR2701",
        name="尿素2701",
        exchange="CZCE",
        multiplier=20.0,
        tick_size=1.0,
        margin_rate=0.08,
        fee_per_lot=5.0,
        fee_rate=0.0,
        last_trade_date="2027-01-15",
    ),
    "RU2701": ContractSpec(
        symbol="RU2701",
        name="天然橡胶2701",
        exchange="SHFE",
        multiplier=10.0,
        tick_size=5.0,
        margin_rate=0.10,
        fee_per_lot=3.0,
        fee_rate=0.0,
        last_trade_date="2027-01-15",
    ),
    "Y2701": ContractSpec(
        symbol="Y2701",
        name="豆油2701",
        exchange="DCE",
        multiplier=10.0,
        tick_size=2.0,
        margin_rate=0.08,
        fee_per_lot=2.5,
        fee_rate=0.0,
        last_trade_date="2027-01-15",
    ),
    "P2701": ContractSpec(
        symbol="P2701",
        name="棕榈油2701",
        exchange="DCE",
        multiplier=10.0,
        tick_size=2.0,
        margin_rate=0.09,
        fee_per_lot=2.5,
        fee_rate=0.0,
        last_trade_date="2027-01-15",
    ),
    "JD2701": ContractSpec(
        symbol="JD2701",
        name="鸡蛋2701",
        exchange="DCE",
        multiplier=10.0,
        tick_size=1.0,
        margin_rate=0.08,
        fee_per_lot=2.5,
        fee_rate=0.0,
        last_trade_date="2027-01-15",
    ),
    "AP2701": ContractSpec(
        symbol="AP2701",
        name="苹果2701",
        exchange="CZCE",
        multiplier=10.0,
        tick_size=1.0,
        margin_rate=0.10,
        fee_per_lot=5.0,
        fee_rate=0.0,
        last_trade_date="2027-01-15",
    ),
    # ---- 国际代表（新浪 hf_ 连续/主力近似；参数为模拟近似值） ----
    "OIL": ContractSpec(
        symbol="OIL",
        name="布伦特原油",
        exchange="ICE",
        multiplier=1000.0,
        tick_size=0.01,
        margin_rate=0.10,
        fee_per_lot=2.0,
        fee_rate=0.0,
        last_trade_date="2030-12-31",
    ),
    "GC": ContractSpec(
        symbol="GC",
        name="COMEX黄金",
        exchange="COMEX",
        multiplier=100.0,
        tick_size=0.10,
        margin_rate=0.05,
        fee_per_lot=2.5,
        fee_rate=0.0,
        last_trade_date="2030-12-31",
    ),
    "SI": ContractSpec(
        symbol="SI",
        name="COMEX白银",
        exchange="COMEX",
        multiplier=5000.0,
        tick_size=0.005,
        margin_rate=0.07,
        fee_per_lot=2.5,
        fee_rate=0.0,
        last_trade_date="2030-12-31",
    ),
    "CAD": ContractSpec(
        symbol="CAD",
        name="LME铜",
        exchange="LME",
        multiplier=25.0,
        tick_size=0.5,
        margin_rate=0.10,
        fee_per_lot=2.5,
        fee_rate=0.0,
        last_trade_date="2030-12-31",
    ),
    "S": ContractSpec(
        symbol="S",
        name="CBOT大豆",
        exchange="CBOT",
        multiplier=5000.0,
        tick_size=0.0025,
        margin_rate=0.07,
        fee_per_lot=2.5,
        fee_rate=0.0,
        last_trade_date="2030-12-31",
    ),
    "C": ContractSpec(
        symbol="C",
        name="CBOT玉米",
        exchange="CBOT",
        multiplier=5000.0,
        tick_size=0.0025,
        margin_rate=0.07,
        fee_per_lot=2.5,
        fee_rate=0.0,
        last_trade_date="2030-12-31",
    ),
    "W": ContractSpec(
        symbol="W",
        name="CBOT小麦",
        exchange="CBOT",
        multiplier=5000.0,
        tick_size=0.0025,
        margin_rate=0.07,
        fee_per_lot=2.5,
        fee_rate=0.0,
        last_trade_date="2030-12-31",
    ),
    "CT": ContractSpec(
        symbol="CT",
        name="ICE棉花",
        exchange="ICE",
        multiplier=50000.0,
        tick_size=0.0001,
        margin_rate=0.07,
        fee_per_lot=2.5,
        fee_rate=0.0,
        last_trade_date="2030-12-31",
    ),
    "KC": ContractSpec(
        symbol="KC",
        name="ICE咖啡",
        exchange="ICE",
        multiplier=37500.0,
        tick_size=0.0005,
        margin_rate=0.10,
        fee_per_lot=2.5,
        fee_rate=0.0,
        last_trade_date="2030-12-31",
    ),
}

# 各品种夜盘结束时间不同（近似交易所规则）：23:00 / 01:00 / 02:30
_CN_SESSION_PROFILES = {
    "RB2701": "night_2300",
    "I2701": "night_2300",
    "M2701": "night_2300",
    "SR2701": "night_2300",
    "CF2701": "night_2300",
    "MA2701": "night_2300",
    "FG2701": "night_2300",
    "SA2701": "night_2300",
    "UR2701": "night_2300",
    "RU2701": "night_2300",
    "Y2701": "night_2300",
    "P2701": "night_2300",
    "CU2612": "night_0100",
    "AL2612": "night_0100",
    "SC2701": "night_0230",
    "AU2612": "night_0230",
    "AG2612": "night_0230",
}
for _symbol, _profile in _CN_SESSION_PROFILES.items():
    if _symbol in DEFAULT_CONTRACTS:
        DEFAULT_CONTRACTS[_symbol] = replace(
            DEFAULT_CONTRACTS[_symbol], session_profile=_profile
        )

# 国内主力换月链：当前合约 -> 下一主力合约（参数复用，仅代码/名称/到期日变化）
_ROLLOVER_NEXT: dict[str, tuple[str, str, str]] = {
    "RB2701": ("RB2705", "螺纹钢2705", "2027-05-15"),
    "M2701": ("M2705", "豆粕2705", "2027-05-14"),
    "AU2612": ("AU2702", "沪金2702", "2027-02-15"),
    "AG2612": ("AG2702", "沪银2702", "2027-02-15"),
    "CU2612": ("CU2701", "沪铜2701", "2027-01-15"),
    "AL2612": ("AL2701", "沪铝2701", "2027-01-15"),
    "I2701": ("I2705", "铁矿石2705", "2027-05-15"),
    "SR2701": ("SR2705", "白糖2705", "2027-05-15"),
    "CF2701": ("CF2705", "棉花2705", "2027-05-15"),
    "SC2701": ("SC2702", "原油2702", "2027-01-29"),
    "MA2701": ("MA2705", "甲醇2705", "2027-05-14"),
    "LH2701": ("LH2705", "生猪2705", "2027-05-14"),
    "FG2701": ("FG2705", "玻璃2705", "2027-05-14"),
    "SA2701": ("SA2705", "纯碱2705", "2027-05-14"),
    "UR2701": ("UR2705", "尿素2705", "2027-05-14"),
    "RU2701": ("RU2705", "橡胶2705", "2027-05-14"),
    "Y2701": ("Y2705", "豆油2705", "2027-05-14"),
    "P2701": ("P2705", "棕榈油2705", "2027-05-14"),
    "JD2701": ("JD2705", "鸡蛋2705", "2027-05-14"),
    "AP2701": ("AP2705", "苹果2705", "2027-05-14"),
}
for _old, (_new, _name, _last_date) in _ROLLOVER_NEXT.items():
    if _old not in DEFAULT_CONTRACTS:
        continue
    _base = DEFAULT_CONTRACTS[_old]
    DEFAULT_CONTRACTS[_new] = replace(
        _base,
        symbol=_new,
        name=_name,
        last_trade_date=_last_date,
        rollover_to="",
    )
    DEFAULT_CONTRACTS[_old] = replace(_base, rollover_to=_new)

# 第二级换月：下一主力 -> 再下一主力（多级链；按相同品种参数生成）
_ROLLOVER_NEXT2: dict[str, tuple[str, str, str]] = {
    "RB2705": ("RB2710", "螺纹钢2710", "2027-10-15"),
    "M2705": ("M2709", "豆粕2709", "2027-09-14"),
    "AU2702": ("AU2704", "沪金2704", "2027-04-15"),
    "AG2702": ("AG2704", "沪银2704", "2027-04-15"),
    "CU2701": ("CU2702", "沪铜2702", "2027-02-15"),
    "AL2701": ("AL2702", "沪铝2702", "2027-02-15"),
    "I2705": ("I2709", "铁矿石2709", "2027-09-15"),
    "SR2705": ("SR2709", "白糖2709", "2027-09-15"),
    "CF2705": ("CF2709", "棉花2709", "2027-09-15"),
    "SC2702": ("SC2703", "原油2703", "2027-02-26"),
    "MA2705": ("MA2709", "甲醇2709", "2027-09-14"),
    "LH2705": ("LH2709", "生猪2709", "2027-09-14"),
    "FG2705": ("FG2709", "玻璃2709", "2027-09-14"),
    "SA2705": ("SA2709", "纯碱2709", "2027-09-14"),
    "UR2705": ("UR2709", "尿素2709", "2027-09-14"),
    "RU2705": ("RU2709", "橡胶2709", "2027-09-14"),
    "Y2705": ("Y2709", "豆油2709", "2027-09-14"),
    "P2705": ("P2709", "棕榈油2709", "2027-09-14"),
    "JD2705": ("JD2709", "鸡蛋2709", "2027-09-14"),
    "AP2705": ("AP2710", "苹果2710", "2027-10-14"),
}
for _old, (_new, _name, _last_date) in _ROLLOVER_NEXT2.items():
    if _old not in DEFAULT_CONTRACTS:
        continue
    _base = DEFAULT_CONTRACTS[_old]
    DEFAULT_CONTRACTS[_new] = replace(
        _base,
        symbol=_new,
        name=_name,
        last_trade_date=_last_date,
        rollover_to="",
    )
    DEFAULT_CONTRACTS[_old] = replace(_base, rollover_to=_new)


@dataclass(frozen=True)
class MarketProfile:
    """一个“市场档案”：用户只选市场，插件内部维护品种池、币种与时段。"""

    id: str
    label: str
    source: str
    contracts: tuple[str, ...]
    description: str = ""
    quote_style: str = "nf"  # nf = 新浪国内期货，hf = 新浪国际期货
    currency: str = "CNY"
    initial_balance: float = 1_000_000.0
    session_entries: tuple[str, ...] | None = None  # None = 用 market.sessions
    night_enabled: bool | None = None
    trading_day: str = "cn"  # cn = 夜盘归下一交易日，calendar = 按自然日
    settle_enabled: bool = True
    settle_time: str = "15:10"
    settle_use_last: bool = False


MARKET_PROFILES: dict[str, MarketProfile] = {
    "cn": MarketProfile(
        id="cn",
        label="国内商品期货",
        source="sina",
        contracts=(
            "SC2701",
            "AU2612",
            "AG2612",
            "CU2612",
            "AL2612",
            "RB2701",
            "I2701",
            "M2701",
            "SR2701",
            "CF2701",
            "MA2701",
            "LH2701",
            "FG2701",
            "SA2701",
            "UR2701",
            "RU2701",
            "Y2701",
            "P2701",
            "JD2701",
            "AP2701",
        ),
        description="新浪 nf_ 国内商品期货；按能源/贵金属/有色/黑色/化工/农产品大类，每类保留代表品种",
        quote_style="nf",
        currency="麦麦币",
        initial_balance=1_000_000.0,
        session_entries=None,
        night_enabled=None,
        trading_day="cn",
        settle_enabled=True,
        settle_time="15:10",
        settle_use_last=False,
    ),
    "intl": MarketProfile(
        id="intl",
        label="国际市场",
        source="sina",
        contracts=("OIL", "GC", "SI", "CAD", "S", "C", "W", "CT", "KC"),
        description="新浪 hf_ 国际期货；布伦特/COMEX金银/LME铜/CBOT农产品（连续近似）",
        quote_style="hf",
        currency="麦麦币",
        initial_balance=200_000.0,
        session_entries=("00:00-05:00", "06:00-23:59"),
        night_enabled=False,
        trading_day="calendar",
        settle_enabled=True,
        settle_time="05:10",
        settle_use_last=True,
    ),
}
_MARKET_ALIASES = {
    "cn": "cn",
    "domestic": "cn",
    "国内": "cn",
}


_CATEGORY_BY_PRODUCT: dict[str, str] = {
    "SC": "能源", "OIL": "能源",
    "AU": "贵金属", "AG": "贵金属", "GC": "贵金属", "SI": "贵金属",
    "CU": "有色", "AL": "有色", "CAD": "有色",
    "RB": "黑色", "I": "黑色",
    "MA": "化工", "FG": "化工", "SA": "化工", "UR": "化工", "RU": "化工",
    "M": "农产品", "SR": "农产品", "CF": "农产品",
    "S": "农产品", "C": "农产品", "W": "农产品", "CT": "农产品", "KC": "农产品",
    "LH": "农产品", "Y": "农产品", "P": "农产品", "JD": "农产品", "AP": "农产品",
}

# 前台展示名：不带合约月份；经典品种保留市场前缀
_FRONT_NAMES: dict[str, str] = {
    "SC": "原油", "AU": "黄金", "AG": "白银", "CU": "铜", "AL": "铝",
    "RB": "螺纹钢", "I": "铁矿石", "M": "豆粕", "SR": "白糖", "CF": "棉花",
    "MA": "甲醇", "LH": "生猪", "FG": "玻璃", "SA": "纯碱", "UR": "尿素",
    "RU": "橡胶", "Y": "豆油", "P": "棕榈油", "JD": "鸡蛋", "AP": "苹果",
    "OIL": "布伦特原油", "GC": "COMEX黄金", "SI": "COMEX白银", "CAD": "LME铜",
    "S": "CBOT大豆", "C": "CBOT玉米", "W": "CBOT小麦",
    "CT": "ICE棉花", "KC": "ICE咖啡",
}


def front_name(symbol: str) -> str:
    """前台展示名（不带月份/代码）；未知合约退回合约名。"""

    text = str(symbol or "").upper()
    product = "".join(ch for ch in text if ch.isalpha())
    if product in _FRONT_NAMES:
        return _FRONT_NAMES[product]
    spec = DEFAULT_CONTRACTS.get(text)
    if spec is not None:
        return "".join(ch for ch in spec.name if not ch.isdigit()) or text
    return text


def category_for(symbol: str) -> str:
    """按品种代码返回大类（用于市场概览）。"""

    product = "".join(ch for ch in str(symbol or "").upper() if ch.isalpha())
    return _CATEGORY_BY_PRODUCT.get(product, "其他")


for _symbol, _spec in list(DEFAULT_CONTRACTS.items()):
    if not _spec.display_name:
        DEFAULT_CONTRACTS[_symbol] = replace(
            _spec, display_name=front_name(_symbol)
        )

# 让高级品种覆盖也能填中文品种名（如“螺纹钢”“布伦特原油”），
# 不用逼普通用户记 RB2701/SC2701 这类代码。
_ALIAS_TO_SYMBOL: dict[str, str] = {}
for _profile in MARKET_PROFILES.values():
    for _symbol in _profile.contracts:
        _spec = DEFAULT_CONTRACTS.get(_symbol)
        if _spec is None:
            continue
        for _alias in (_symbol, _spec.display_name, front_name(_symbol), _spec.name):
            _text = str(_alias or "").strip()
            if _text:
                _ALIAS_TO_SYMBOL.setdefault(_text.upper(), _symbol)
                _ALIAS_TO_SYMBOL.setdefault(_text, _symbol)


def resolve_market_contracts(
    markets: list[str],
    symbols: list[str] | None = None,
    table: dict[str, ContractSpec] | None = None,
) -> dict[str, ContractSpec]:
    """按“市场”解析合约池。

    - ``symbols`` 非空时视为高级覆盖（保留旧配置兼容）；
    - 否则按 ``markets`` 展开内置市场档案；
    - 未知市场直接报错，避免静默变成空池。
    """

    if symbols:
        return load_contracts(list(symbols), table)
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in markets or []:
        key = str(raw).strip().lower()
        if not key:
            continue
        market_id = _MARKET_ALIASES.get(key, key)
        profile = MARKET_PROFILES.get(market_id)
        if profile is None:
            raise ContractError(f"未知市场：{raw}（当前支持 {sorted(MARKET_PROFILES)}）")
        for symbol in profile.contracts:
            if symbol not in seen:
                seen.add(symbol)
                ordered.append(symbol)
    if not ordered:
        raise ContractError("未选择交易市场")
    return load_contracts(ordered, table)


def load_contracts(
    symbols: list[str],
    table: dict[str, ContractSpec] | None = None,
) -> dict[str, ContractSpec]:
    """按配置的合约池构造 {symbol: spec}，未知合约直接报错。"""

    source = DEFAULT_CONTRACTS if table is None else table
    result: dict[str, ContractSpec] = {}
    for raw_symbol in symbols:
        raw_text = str(raw_symbol).strip()
        symbol = raw_text.upper()
        if not symbol:
            raise ContractError("品种代码不能为空")
        if symbol not in source:
            symbol = _ALIAS_TO_SYMBOL.get(raw_text) or _ALIAS_TO_SYMBOL.get(symbol) or symbol
        if symbol in result:
            continue
        spec = source.get(symbol)
        if spec is None:
            examples = "、".join(
                item.display_name or item_symbol for item_symbol, item in list(source.items())[:8]
            )
            raise ContractError(
                f"未知品种：{raw_text}（可填品种名或合约代码，例如 {examples}）"
            )
        result[symbol] = spec
    if not result:
        raise ContractError("品种池为空")
    return result


def get_contract(symbol: str, table: dict[str, ContractSpec] | None = None) -> ContractSpec:
    """按代码取合约参数；未知合约抛 ContractError。"""

    source = DEFAULT_CONTRACTS if table is None else table
    normalized = str(symbol).strip().upper()
    spec = source.get(normalized)
    if spec is None:
        raise ContractError(f"未知合约：{normalized}")
    return spec
