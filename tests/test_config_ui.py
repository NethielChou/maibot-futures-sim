"""WebUI 配置页中文化/分组回归测试。"""

import unittest

from tests.support import load_package


class ConfigUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        package = load_package()
        cls.plugin = package.create_plugin()
        cls.schema = type(cls.plugin).build_config_schema(
            plugin_id="github.nethielchou.mai-futures-sim",
            plugin_name="麦麦期货模拟盘",
            plugin_version="0.8.7",
        )

    def _field(self, section_id: str, field_name: str) -> dict:
        return self.schema["sections"][section_id]["fields"][field_name]

    def test_tabs_and_sections_are_chinese(self):
        tabs = self.schema["layout"]["tabs"]
        self.assertEqual([tab["title"] for tab in tabs], ["常用", "交易", "高级"])
        self.assertIn("account", self.schema["sections"])
        self.assertIn("market_core", self.schema["sections"])
        self.assertIn("market_advanced", self.schema["sections"])
        self.assertIn("risk_core", self.schema["sections"])
        self.assertIn("decision_core", self.schema["sections"])

    def test_common_field_labels_are_chinese(self):
        expected = {
            ("account", "initial_balance"): "起始资金（麦麦币）",
            ("market_core", "markets"): "交易市场",
            ("risk_core", "max_total_margin_ratio"): "总保证金上限（权益比例）",
            ("decision_core", "auto_trade"): "自动交易",
            ("notify", "group_ids"): "播报群号",
            ("admin", "users"): "管理员账号",
        }
        for (section_id, field_name), label in expected.items():
            self.assertEqual(self._field(section_id, field_name)["label"], label)

    def test_non_programmer_hints_exist(self):
        markets = self._field("market_core", "markets")
        self.assertIn("cn", markets["hint"])
        self.assertIn("intl", markets["hint"])
        symbols = self._field("market_core", "symbols")
        self.assertIn("留空", symbols["hint"])
        group_ids = self._field("notify", "group_ids")
        self.assertIn("群号", group_ids["hint"])

    def test_unsafe_or_developer_fields_are_disabled_or_hidden(self):
        self.assertTrue(self._field("plugin", "config_version")["disabled"])
        self.assertTrue(self._field("market_advanced", "source")["disabled"])
        # 未使用字段不在布局里，避免普通用户误调
        self.assertNotIn("storage", self.schema["sections"])
        self.assertNotIn(
            "close_report_delay_minutes",
            self.schema["sections"]["notify"]["fields"],
        )


if __name__ == "__main__":
    unittest.main()
