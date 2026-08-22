from __future__ import annotations

import unittest

from src.structure import StructureError, apply_structure_overrides, detect_structure


ARTICLE = """市府啟動河岸更新計畫
首階段預計明年完工

市府今天宣布啟動河岸更新，第一階段將改善步道與照明。

計畫背景

工程局表示，預算為新台幣三億元，資料來源為市府公告。

後續影響

沿線居民要求施工期間保留通行空間。"""


class StructureTests(unittest.TestCase):
    def test_detects_five_newsroom_types_with_exact_coordinates(self) -> None:
        result = detect_structure(ARTICLE)
        self.assertEqual(
            [item["type"] for item in result["blocks"]],
            ["main_title", "subtitle", "lead", "subheading", "body", "subheading", "body"],
        )
        self.assertEqual(result["summary"]["subheading"], 2)
        for item in result["blocks"]:
            self.assertEqual(ARTICLE[item["start"] : item["end"]], item["text"])

    def test_block_ids_are_stable_and_crlf_coordinates_still_slice(self) -> None:
        first = detect_structure(ARTICLE)
        second = detect_structure(ARTICLE)
        self.assertEqual(
            [item["id"] for item in first["blocks"]],
            [item["id"] for item in second["blocks"]],
        )
        crlf = ARTICLE.replace("\n", "\r\n")
        result = detect_structure(crlf)
        for item in result["blocks"]:
            self.assertEqual(crlf[item["start"] : item["end"]], item["text"])

    def test_human_override_preserves_text_and_coordinates(self) -> None:
        detected = detect_structure(ARTICLE)
        target = next(item for item in detected["blocks"] if item["type"] == "subheading")
        updated = apply_structure_overrides(
            ARTICLE,
            detected,
            [{"id": target["id"], "type": "body"}],
        )
        changed = next(item for item in updated["blocks"] if item["id"] == target["id"])
        self.assertEqual(changed["type"], "body")
        self.assertEqual(changed["source"], "human")
        self.assertEqual(changed["text"], target["text"])
        self.assertEqual((changed["start"], changed["end"]), (target["start"], target["end"]))

    def test_rejects_unknown_block_and_type(self) -> None:
        detected = detect_structure(ARTICLE)
        with self.assertRaises(StructureError):
            apply_structure_overrides(ARTICLE, detected, [{"id": "missing", "type": "body"}])
        with self.assertRaises(StructureError):
            apply_structure_overrides(
                ARTICLE,
                detected,
                [{"id": detected["blocks"][0]["id"], "type": "invented"}],
            )


if __name__ == "__main__":
    unittest.main()
