from __future__ import annotations

import unittest

from src.newsroom import (
    extract_source_clues,
    merge_source_decisions,
    rewrite_with_persona,
    source_readiness,
)


class NewsroomWorkflowTests(unittest.TestCase):
    def test_source_clues_require_explicit_decisions(self) -> None:
        clues = extract_source_clues("市府表示工程仍在規畫。\n來源：市府公開資料")
        merged = merge_source_decisions(clues, [])
        self.assertGreaterEqual(len(merged), 2)
        self.assertFalse(source_readiness(merged)["ready"])

        decided = [{**item, "status": "confirmed", "note": ""} for item in merged]
        self.assertTrue(source_readiness(decided)["ready"])

    def test_gap_and_unexplained_doubt_block_readiness(self) -> None:
        missing_clues = extract_source_clues("稿件沒有任何歸因")
        confirmed_missing = [{**missing_clues[0], "status": "confirmed", "note": ""}]
        self.assertFalse(source_readiness(confirmed_missing)["ready"])
        gap = [{**missing_clues[0], "status": "gap", "note": ""}]
        self.assertFalse(source_readiness(gap)["ready"])
        clues = extract_source_clues("市府表示將公告。")
        doubt = [{**clues[0], "status": "doubt", "note": ""}]
        self.assertFalse(source_readiness(doubt)["ready"])
        doubt[0]["note"] = "等待第二來源交叉確認"
        self.assertTrue(source_readiness(doubt)["ready"])

    def test_persona_rewrite_is_conservative_and_traceable(self) -> None:
        source = "市府  表示！！將打造全方位服務。\n來源: 市府公告"
        result = rewrite_with_persona(source, "editor.news")
        self.assertEqual(result["persona_id"], "editor.news")
        self.assertIn("市府", result["content"])
        self.assertIn("來源：", result["content"])
        self.assertTrue(result["changed"])


if __name__ == "__main__":
    unittest.main()
