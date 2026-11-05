"""开发脚本：从配置模型生成 config.example.toml，并生成 icon.png。

用法（必须用 Bot Python 运行）：
  <generation 目录下的 python.exe> scripts/dev_bootstrap_artifacts.py

不访问网络、不接触宿主运行数据。
"""

import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_NAME = "_maibot_plugin_dev_bootstrap"


def load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME,
        str(PROJECT_ROOT / "plugin.py"),
        submodule_search_locations=[str(PROJECT_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法创建插件模块 spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def write_config_example(defaults: dict) -> Path:
    import tomlkit

    header = (
        "# 麦麦期货模拟盘 config.example.toml\n"
        "# 由 scripts/dev_bootstrap_artifacts.py 从 config_model 默认值生成，请勿手改本文件。\n"
        "# 复制为 config.toml 后由宿主加载；仓库根目录不得提交 config.toml。\n\n"
    )
    path = PROJECT_ROOT / "config.example.toml"
    path.write_text(header + tomlkit.dumps(defaults), encoding="utf-8")
    return path


def write_icon() -> Path:
    from PIL import Image, ImageDraw

    width = height = 256
    image = Image.new("RGB", (width, height), "#12324A")
    draw = ImageDraw.Draw(image)

    # 背景网格
    for x in range(0, width, 32):
        draw.line([(x, 0), (x, height)], fill="#1B3E5C", width=1)
    for y in range(0, height, 32):
        draw.line([(0, y), (width, y)], fill="#1B3E5C", width=1)

    # 上涨烛台（橙 + 红）
    candles = [
        (48, 150, 96, 210, "#F3B27A"),
        (96, 120, 144, 190, "#F08A5D"),
        (144, 96, 192, 160, "#F3B27A"),
        (192, 60, 240, 130, "#F08A5D"),
    ]
    for left, top, right, bottom, color in candles:
        draw.rectangle([left, top, right, bottom], fill=color)
        mid_x = (left + right) // 2
        draw.line([(mid_x, top - 12), (mid_x, bottom + 12)], fill="#FFFFFF", width=4)

    # 趋势线
    draw.line([(36, 190), (96, 156), (150, 126), (220, 78)], fill="#FFFFFF", width=5)

    # 底部小字标记（纯几何，不含中文字体依赖）
    draw.rectangle([36, 224, 220, 232], fill="#F3B27A")

    path = PROJECT_ROOT / "icon.png"
    image.save(path, format="PNG")
    return path


def main() -> None:
    module = load_plugin_module()
    plugin = module.create_plugin()
    defaults = type(plugin).build_default_config()
    config_path = write_config_example(defaults)
    icon_path = write_icon()
    print(
        json.dumps(
            {
                "config_example": str(config_path),
                "config_sections": list(defaults.keys()),
                "icon": str(icon_path),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
