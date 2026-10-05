"""数据源注册表 + 编排分发（ingest/registry.py + orchestrator.ingest）单测。"""

import unittest

from quantlab.ingest.orchestrator import IngestError, ingest
from quantlab.ingest.registry import SOURCE_REGISTRY

# 烘焙进快照/台账的 source 标签：改了会换 snapshot_id、v_bars_latest 漂移（防迁移回归）
_EXPECTED_TAGS = {
    "synthetic": "synthetic",
    "tushare": "tushare",
    "tushare_index": "tushare_index",
    "tushare_hk": "tushare_hk",
    "tushare_macro": "tushare_macro",
    "futu": "futu",
}


class TestRegistry(unittest.TestCase):
    def test_source_tags_are_stable(self):
        self.assertEqual(set(SOURCE_REGISTRY), set(_EXPECTED_TAGS))
        for source_id, reg in SOURCE_REGISTRY.items():
            self.assertEqual(reg.source_tag, _EXPECTED_TAGS[source_id])
            self.assertEqual(reg.source_id, source_id)

    def test_every_source_has_builder_and_check(self):
        for reg in SOURCE_REGISTRY.values():
            self.assertTrue(callable(reg.builder))
            self.assertTrue(callable(reg.check))


class TestOrchestratorDispatch(unittest.TestCase):
    def test_unknown_source_fails(self):
        with self.assertRaises(IngestError):
            ingest(source="definitely-not-a-source")

    def test_disabled_source_fails(self):
        # config/sources.toml 中 futu 声明 enabled=false，ingest 必须明确报错、不得静默跳过
        with self.assertRaises(IngestError):
            ingest(source="futu")


if __name__ == "__main__":
    unittest.main()
