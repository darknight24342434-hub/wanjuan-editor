from __future__ import annotations

import unittest

from src.review import review_content
from src.video_plan import build_video_plan


class VideoPlanTests(unittest.TestCase):
    def test_builds_traceable_non_dispatchable_draft(self) -> None:
        review = review_content(
            "某政策引發爭議",
            "根據公開資料，某民代質疑2026年度預算，主管機關表示將說明。",
            "chinatimes_newsroom",
            "https://example.test/source",
        )
        result = build_video_plan(
            "某政策引發爭議",
            "根據公開資料，某民代質疑2026年度預算，主管機關表示將說明。",
            "https://example.test/source",
            review,
            "review-run-1",
        )

        self.assertEqual(result["status"]["code"], "draft_not_dispatchable")
        self.assertEqual(result["provenance"]["review_run_id"], "review-run-1")
        self.assertEqual([item["name"] for item in result["platform_versions"]], ["30 秒", "60 秒", "90 秒", "YouTube 長版"])
        self.assertTrue(any(item["id"] == "semantic_review" for item in result["risk_gate"]))
        self.assertIn("未呼叫語義模型", result["notice"])

    def test_missing_source_and_response_are_explicit_risks(self) -> None:
        review = review_content("爭議稿", "某人士痛批政策荒謬。", "chinatimes_newsroom")
        result = build_video_plan("爭議稿", "某人士痛批政策荒謬。", "", review, "review-run-2")
        risk_ids = {item["id"] for item in result["risk_gate"]}
        self.assertIn("source", risk_ids)
        self.assertIn("response", risk_ids)
        self.assertIn("material", risk_ids)

    def test_rejects_empty_document(self) -> None:
        with self.assertRaisesRegex(ValueError, "沒有正文"):
            build_video_plan("空白", "", "", {}, "review-run-3")


if __name__ == "__main__":
    unittest.main()
