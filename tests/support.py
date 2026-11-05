"""测试辅助：按宿主方式加载插件包、构造行情 fixture、临时账本。"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "_maibot_plugin_mai_futures_test_pkg"
CST = ZoneInfo("Asia/Shanghai")


def load_package() -> object:
    """按宿主 plugin_loader 的方式加载 plugin.py 为包，并注册到 sys.modules。"""

    if PACKAGE_NAME in sys.modules:
        return sys.modules[PACKAGE_NAME]
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        str(PROJECT_ROOT / "plugin.py"),
        submodule_search_locations=[str(PROJECT_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法创建插件模块 spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module


def submodule(name: str):
    """导入插件包内的子模块（如 ``quotes`` / ``ledger``）。"""

    load_package()
    return importlib.import_module(f"{PACKAGE_NAME}.{name}")


def make_commodity_line(
    symbol: str = "RB2701",
    *,
    name: str = "螺纹钢2701",
    time_text: str = "100000",
    date_text: str = "2026-11-03",
    last: str = "3007.000",
    bid1: str = "3006.000",
    ask1: str = "3007.000",
    settle: str = "0.000",
    prev_settle: str = "3039.000",
) -> str:
    """构造一条 44 字段的商品期货行情行（字段布局与实测一致）。"""

    fields = [""] * 44
    fields[0] = name
    fields[1] = time_text
    fields[2] = "3030.000"
    fields[3] = "3032.000"
    fields[4] = "3006.000"
    fields[5] = "3007.000"
    fields[6] = bid1
    fields[7] = ask1
    fields[8] = last
    fields[9] = settle
    fields[10] = prev_settle
    fields[11] = "1844"
    fields[12] = "86"
    fields[13] = "1866893.000"
    fields[14] = "715629"
    fields[15] = "沪"
    fields[16] = "螺纹钢"
    fields[17] = date_text
    fields[18] = "1"
    fields[28] = "3019.819"
    for index in range(29, 44):
        fields[index] = "1"
    return f'var hq_str_nf_{symbol}="' + ",".join(fields) + '";'


def make_quote(
    *,
    symbol: str = "RB2701",
    now: datetime | None = None,
    last: float = 3007.0,
    bid1: float = 3006.0,
    ask1: float = 3007.0,
    settle: float = 0.0,
    age_seconds: float = 1.0,
    prev_settle: float = 3039.0,
    prev_close: float = 3007.0,
):
    """直接构造标准化 Quote（用于成交/风控/账本测试）。"""

    quotes_module = submodule("quotes")
    base = now or datetime(2026, 11, 3, 10, 0, 0, tzinfo=CST)
    quote_time = base - timedelta(seconds=age_seconds)
    return quotes_module.Quote(
        symbol=symbol,
        name="螺纹钢2701",
        quote_time=quote_time,
        received_at=base,
        open=3030.0,
        high=3032.0,
        low=3006.0,
        prev_close=prev_close,
        bid1=bid1,
        ask1=ask1,
        last=last,
        settle=settle,
        prev_settle=prev_settle,
        bid1_volume=1844,
        ask1_volume=86,
        open_interest=1866893.0,
        volume=715629.0,
    )


def temp_data_dir(tmp_path: Path) -> Path:
    """返回测试用插件数据目录。"""

    path = tmp_path / "data" / "plugins" / "github.nethielchou.mai-futures-sim"
    path.mkdir(parents=True, exist_ok=True)
    return path
