from __future__ import annotations

import unittest

from src.review import review_content


class ReviewTests(unittest.TestCase):
    def test_detects_ai_phrase_and_repeated_punctuation(self) -> None:
        result = review_content("測試", "我們要深入探討這個關鍵問題！！")
        categories = [item["category"] for item in result["suggestions"]]
        self.assertIn("AI 痕跡", categories)
        self.assertIn("標點", categories)
        self.assertGreaterEqual(result["summary"]["warnings"], 3)

    def test_reports_stats_without_external_ai_claim(self) -> None:
        result = review_content("標題", "第一段。\n\n第二段。")
        self.assertEqual(result["engine"], "local_rules")
        self.assertEqual(result["stats"]["paragraphs"], 2)
        self.assertIn("未呼叫外部 AI", result["notice"])

    def test_newsroom_preflight_exposes_unfinished_semantic_stages_and_personas(self) -> None:
        result = review_content("某政策引發爭議", "某民代痛批政策錯誤，涉及2026年度預算。", "chinatimes_newsroom")
        self.assertEqual(result["workflow"]["id"], "chinatimes_newsroom")
        self.assertTrue(any(stage["status"] == "semantic_model_required" for stage in result["workflow"]["stages"]))
        persona_ids = [item["id"] for item in result["rewrite_personas"]]
        self.assertIn("editor.news", persona_ids)
        self.assertIn("editor.baigui_editor_in_chief", persona_ids)
        categories = [item["category"] for item in result["suggestions"]]
        self.assertIn("同項回應", categories)


if __name__ == "__main__":
    unittest.main()
