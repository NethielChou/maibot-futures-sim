"""G2 骨架契约测试（必须在 Bot Python 下运行）。

覆盖面：
1. 按宿主方式（spec_from_file_location + submodule_search_locations + sys.modules）加载包；
2. create_plugin() / 生命周期覆写 / 默认配置 / 组件注册 / 配置 Schema；
3. 真实宿主 ManifestValidator 校验 _manifest.json；
4. config.example.toml 与配置模型默认值一致。
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_MODULE_NAME = "_maibot_plugin_github_nethielchou_mai_futures_sim_smoke"


def load_plugin_module():
    """按宿主 plugin_loader 的方式加载插件包。"""

    sys.modules.pop(PLUGIN_MODULE_NAME, None)
    spec = importlib.util.spec_from_file_location(
        PLUGIN_MODULE_NAME,
        str(PROJECT_ROOT / "plugin.py"),
        submodule_search_locations=[str(PROJECT_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法创建插件模块 spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PLUGIN_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


class PluginContractTest(unittest.TestCase):
    """宿主加载契约。"""

    @classmethod
    def setUpClass(cls):
        cls.module = load_plugin_module()
        cls.plugin = cls.module.create_plugin()

    def test_create_plugin(self):
        from maibot_sdk.plugin import MaiBotPlugin

        self.assertIsInstance(self.plugin, MaiBotPlugin)
        self.assertTrue(callable(self.module.create_plugin))

    def test_lifecycle_overridden(self):
        from maibot_sdk.plugin import MaiBotPlugin

        plugin_type = type(self.plugin)
        self.assertIsNot(plugin_type.on_load, MaiBotPlugin.on_load)
        self.assertIsNot(plugin_type.on_unload, MaiBotPlugin.on_unload)
        self.assertIsNot(plugin_type.on_config_update, MaiBotPlugin.on_config_update)

    def test_default_config(self):
        config = type(self.plugin).build_default_config()
        self.assertEqual(config["plugin"]["config_version"], "0.8.4")
        for section in ("plugin", "account", "market", "risk", "decision", "notify", "admin", "influence", "storage"):
            self.assertIn(section, config)
        self.assertEqual(config["market"]["markets"], ["cn"])
        self.assertEqual(config["market"]["symbols"], [])
        self.assertFalse(config["decision"]["auto_trade"])
        self.assertEqual(config["notify"]["group_ids"], [])

    def test_config_schema(self):
        schema = type(self.plugin).build_config_schema(
            plugin_id="github.nethielchou.mai-futures-sim",
            plugin_name="麦麦期货模拟盘",
            plugin_version="0.8.4",
        )
        self.assertIsInstance(schema, dict)
        self.assertTrue(schema)

    def test_components_registered(self):
        components = self.plugin.get_components()
        self.assertIsInstance(components, list)
        dump = json.dumps(components, ensure_ascii=False, default=str)
        self.assertIn("期货", dump)

    def test_manifest_static_checks(self):
        import re

        raw = json.loads((PROJECT_ROOT / "_manifest.json").read_text(encoding="utf-8"))
        required = (
            "manifest_version",
            "id",
            "version",
            "name",
            "description",
            "author",
            "license",
            "urls",
            "host_application",
            "sdk",
            "capabilities",
            "i18n",
        )
        for field in required:
            self.assertIn(field, raw)
        self.assertEqual(raw["manifest_version"], 2)
        self.assertRegex(raw["version"], r"^\d+\.\d+\.\d+$")
        for key in ("host_application", "sdk"):
            version_range = raw[key]
            self.assertRegex(version_range["min_version"], r"^\d+\.\d+\.\d+$")
            self.assertRegex(version_range["max_version"], r"^\d+\.\d+\.\d+$")
            self.assertLessEqual(
                tuple(int(part) for part in version_range["min_version"].split(".")),
                tuple(int(part) for part in version_range["max_version"].split(".")),
            )
        self.assertTrue(raw["urls"]["repository"].startswith("https://github.com/"))
        icon = raw["display"]["icon"]
        self.assertEqual(icon["type"], "local")
        self.assertTrue((PROJECT_ROOT / icon["value"]).is_file(), "本地图标文件不存在")
        self.assertIsInstance(raw["capabilities"], list)
        self.assertIn("send.forward", raw["capabilities"])
        self.assertIn("send.text", raw["capabilities"])
        self.assertEqual(raw["i18n"]["default_locale"], "zh-CN")

    def test_manifest_host_validator(self):
        """真实宿主 ManifestValidator（需要 MAIBOT_INSTANCE_DIR 指向实例根）。"""
        import os

        instance_dir = os.environ.get("MAIBOT_INSTANCE_DIR", "").strip()
        if not instance_dir:
            self.skipTest("未设置 MAIBOT_INSTANCE_DIR，跳过宿主校验（部署前必跑 scripts/dev_verify_manifest.py）")
        from src.plugin_runtime.runner.manifest_validator import ManifestValidator

        raw = json.loads((PROJECT_ROOT / "_manifest.json").read_text(encoding="utf-8"))
        validator = ManifestValidator(
            host_version="1.3.19",
            sdk_version="2.21.0",
            project_root=PROJECT_ROOT,
        )
        self.assertTrue(validator.validate(raw), msg="; ".join(validator.errors))
        self.assertEqual(validator.errors, [])
        loaded = validator.load_from_plugin_path(PROJECT_ROOT, require_entrypoint=True)
        self.assertIsNotNone(loaded, msg="; ".join(validator.errors))

    def test_config_example_matches_defaults(self):
        import tomllib

        example_path = PROJECT_ROOT / "config.example.toml"
        self.assertTrue(example_path.exists(), "config.example.toml 未生成")
        example = tomllib.loads(example_path.read_text(encoding="utf-8"))
        defaults = type(self.plugin).build_default_config()
        self.assertEqual(example["plugin"]["config_version"], defaults["plugin"]["config_version"])
        self.assertEqual(example["market"]["symbols"], defaults["market"]["symbols"])
        self.assertEqual(example["decision"]["max_llm_calls_per_day"], defaults["decision"]["max_llm_calls_per_day"])


if __name__ == "__main__":
    unittest.main()
