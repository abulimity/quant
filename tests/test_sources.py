"""config/sources.toml 配置加载（ingest/sources.py）单测 —— 离线、不联网。"""

import os
import tempfile
import unittest
from pathlib import Path

from quantlab.ingest.sources import load_sources

_SAMPLE = """\
[sources.tushare]
provider = "tushare"
enabled = true
datasets = ["symbols", "bars_daily", "corporate_actions", "fund_adj"]
credentials_env = "TUSHARE_TOKEN"
calendar = "XSHG"

[sources.futu]
provider = "futu"
enabled = false
datasets = ["bars_daily"]
credentials_env = ""
calendar = "XHKG"
currency = "HKD"
notes = "normalize-only"
"""


class TestLoadSources(unittest.TestCase):
    def _write(self, text: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "sources.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_parses_sections_and_fields(self):
        cfg = load_sources(self._write(_SAMPLE))
        self.assertIn("tushare", cfg)
        t = cfg["tushare"]
        self.assertTrue(t.enabled)
        self.assertEqual(t.provider, "tushare")
        self.assertEqual(
            t.datasets, ("symbols", "bars_daily", "corporate_actions", "fund_adj"))
        self.assertEqual(t.credentials_env, "TUSHARE_TOKEN")
        self.assertEqual(t.calendar, "XSHG")

    def test_missing_file_returns_empty(self):
        self.assertEqual(
            load_sources(Path(tempfile.gettempdir()) / "no_such_sources_file.toml"), {})

    def test_strips_and_bools(self):
        cfg = load_sources(self._write(_SAMPLE))
        f = cfg["futu"]
        self.assertFalse(f.enabled)
        self.assertEqual(f.currency, "HKD")

    def test_token_reads_env_only(self):
        cfg = load_sources(self._write(_SAMPLE))
        t = cfg["tushare"]
        self.assertIsNone(t.token())  # 未设置环境变量
        os.environ["TUSHARE_TOKEN"] = "dummy-token"
        self.addCleanup(os.environ.pop, "TUSHARE_TOKEN", None)
        self.assertEqual(t.token(), "dummy-token")

    def test_empty_credentials_env_gives_none(self):
        cfg = load_sources(self._write(_SAMPLE))
        self.assertIsNone(cfg["futu"].token())


if __name__ == "__main__":
    unittest.main()
