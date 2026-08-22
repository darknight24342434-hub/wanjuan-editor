from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path

from src.catalog import BaiguiCatalog, CatalogError


class CatalogTests(unittest.TestCase):
    def make_catalog(self) -> tuple[tempfile.TemporaryDirectory[str], BaiguiCatalog]:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        (root / "cards").mkdir()
        (root / "templates").mkdir()
        (root / "editors").mkdir()
        (root / "cards" / "master.md").write_text(
            "---\nid: style.test\nname: 測試卡\nfamily: literary_master\n"
            "status: original_language_20plus_close_read_complete\n---\n# 測試卡\n",
            encoding="utf-8",
        )
        (root / "templates" / "basic.md").write_text("# 模板", encoding="utf-8")
        (root / "editors" / "editor.md").write_text(
            "---\nid: editor.test\nname: 測試編輯\nrole: 挑錯\nstatus: active\n---\n# 編輯",
            encoding="utf-8",
        )
        (root / "INDEX.yaml").write_text(
            "updated: '2026-08-07'\nactive_assets:\n"
            "  style_cards:\n    - id: style.test\n      name: 測試卡\n      family: literary_master\n      path: cards/master.md\n      status: active\n"
            "  templates:\n    - id: template.test\n      name: 測試模板\n      kind: writing\n      path: templates/basic.md\n      status: active\n"
            "  editors:\n    - id: editor.test\n      name: 測試編輯\n      path: editors/editor.md\n      status: active\n",
            encoding="utf-8",
        )
        return temp, BaiguiCatalog(root)

    def test_loads_assets_and_quality_from_card_frontmatter(self) -> None:
        temp, catalog = self.make_catalog()
        self.addCleanup(temp.cleanup)
        self.assertEqual(catalog.counts(), {"style_cards": 1, "templates": 1, "editors": 1})
        card = catalog.get_asset("style_cards", "style.test")
        self.assertEqual(card["quality_tone"], "complete")
        self.assertIn("完整文本書目", card["quality_label"])
        self.assertTrue(card["complete_text_training"])
        self.assertIn("# 測試卡", card["content"])

    def test_rejects_path_escape(self) -> None:
        temp, _ = self.make_catalog()
        root = Path(temp.name)
        (root / "INDEX.yaml").write_text(
            "active_assets:\n  style_cards:\n    - id: bad\n      name: bad\n      path: ../secret.md\n"
            "  templates: []\n  editors: []\n",
            encoding="utf-8",
        )
        self.addCleanup(temp.cleanup)
        with self.assertRaises(CatalogError):
            BaiguiCatalog(root)

    def test_live_catalog_contract(self) -> None:
        configured = os.environ.get("WENKU_BAIGUI_ROOT", "").strip()
        if not configured:
            self.skipTest("WENKU_BAIGUI_ROOT is not set")
        root = Path(configured)
        if not root.exists():
            self.skipTest("百鬼文庫不在這台主機")
        catalog = BaiguiCatalog(root)
        self.assertEqual(catalog.counts()["style_cards"], 282)
        self.assertEqual(catalog.counts()["templates"], 32)
        self.assertEqual(catalog.counts()["editors"], 11)
        self.assertEqual(catalog.training_summary()["literary_masters"], 138)
        self.assertEqual(catalog.training_summary()["complete_text_20plus"], 13)
        self.assertEqual(catalog.training_summary()["other_literary_masters"], 125)
        all_paths = [item["path"] for item in catalog.list_assets("style_cards")]
        self.assertFalse(any("_draft_missing_qcpass_20260617" in path for path in all_paths))
        self.assertTrue(catalog.has_asset("editors", "editor.news"))
        self.assertTrue(catalog.has_asset("editors", "editor.baigui_editor_in_chief"))

    def test_recommends_plain_language_style_directions(self) -> None:
        configured = os.environ.get("WENKU_BAIGUI_ROOT", "").strip()
        if not configured:
            self.skipTest("WENKU_BAIGUI_ROOT is not set")
        root = Path(configured)
        if not root.exists():
            self.skipTest("百鬼文庫不在這台主機")
        catalog = BaiguiCatalog(root)
        items = catalog.recommend_style_cards("寫一篇台灣職場毒舌短文，不要罵人", 3)
        self.assertGreaterEqual(len(items), 2)
        self.assertTrue(any(item["id"] == "style.genre.taiwan_poison_short" for item in items))
        self.assertTrue(all(item["direction"] for item in items))
        self.assertTrue(all("why" in item for item in items))

    def test_not10plus_is_not_mislabeled_transparent(self) -> None:
        temp, catalog = self.make_catalog()
        self.addCleanup(temp.cleanup)
        root = catalog.root
        card_path = root / "cards" / "master.md"
        card_path.write_text(
            "---\nid: style.test\nname: 測試卡\nfamily: literary_master\n"
            "status: original_author_language_partial_machine_pass_valid6_not10plus\n---\n# 測試卡\n",
            encoding="utf-8",
        )
        catalog.reload()
        card = catalog.get_asset("style_cards", "style.test")
        self.assertEqual(card["training_tier"], "partial_source")
        self.assertNotIn("10+／近 20", card["quality_label"])


if __name__ == "__main__":
    unittest.main()
