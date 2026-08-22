from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from src.repository import DocumentRepository
from src.semantic import (
    EDITOR_FUNCTIONS,
    SemanticContractError,
    SemanticEngineAdapter,
    SemanticFailure,
    SemanticJobManager,
    chief_output_schema,
    editor_output_schema,
    validate_chief_output,
    validate_editor_output,
)
from src.structure import detect_structure


CONTENT = "主標\n副標\n\n這是前言。\n\n小標\n\n這是正文內容。"


def valid_editor(structure: dict) -> dict:
    functions = ("正確性", "可讀性", "文筆", "下標", "結構")
    block_id = structure["blocks"][-1]["id"]
    return {
        "annotations": [
            {
                "id": f"editor-{index}",
                "function": function,
                "severity": "warning" if index == 0 else "note",
                "message": f"{function}建議",
                "excerpt": "",
                "start": None,
                "end": None,
                "structure_block_id": block_id,
            }
            for index, function in enumerate(functions)
        ],
        "headline_candidates": [
            {"id": "headline-1", "main_title": "候選主標一", "subtitle": "候選副標一", "angle": "事件"},
            {"id": "headline-2", "main_title": "候選主標二", "subtitle": "候選副標二", "angle": "影響"},
        ],
    }


def valid_chief(structure: dict) -> dict:
    block_id = structure["blocks"][-1]["id"]
    rows = []
    for index, function in enumerate(("指錯", "糾漏", "建議")):
        rows.append(
            {
                "id": f"chief-{index}",
                "function": function,
                "severity": "warning" if index < 2 else "note",
                "message": f"{function}內容",
                "excerpt": "",
                "start": None,
                "end": None,
                "structure_block_id": block_id,
                "questioned_annotation_id": "editor-0" if function == "指錯" else None,
            }
        )
    recommendation = {"text": "具體建議", "reason": "更清楚"}
    return {
        "annotations": rows,
        "headline_recommendation": recommendation,
        "lead_recommendation": recommendation,
        "angle_recommendation": recommendation,
    }


class FakeCatalog:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def get_asset(self, kind: str, asset_id: str) -> dict:
        self.calls.append((kind, asset_id))
        return {"id": asset_id, "sha256": f"sha-{asset_id}", "content": f"CARD {asset_id}"}


class SemanticTests(unittest.TestCase):
    def test_output_schemas_leave_internal_ids_to_server_normalization(self) -> None:
        schemas = (editor_output_schema(), chief_output_schema())
        editor_annotation = schemas[0]["properties"]["annotations"]["items"]
        headline = schemas[0]["properties"]["headline_candidates"]["items"]
        chief_annotation = schemas[1]["properties"]["annotations"]["items"]

        for item in (editor_annotation, headline, chief_annotation):
            self.assertNotIn("id", item["properties"])

        def assert_strict_required(schema: object) -> None:
            if isinstance(schema, dict):
                properties = schema.get("properties")
                if isinstance(properties, dict):
                    self.assertEqual(set(schema.get("required", [])), set(properties))
                for value in schema.values():
                    assert_strict_required(value)
            elif isinstance(schema, list):
                for value in schema:
                    assert_strict_required(value)

        for schema in schemas:
            assert_strict_required(schema)

    def test_strict_contract_and_editor_link_validation(self) -> None:
        structure = detect_structure(CONTENT)
        editor = validate_editor_output(valid_editor(structure), CONTENT, structure)
        chief = validate_chief_output(valid_chief(structure), CONTENT, structure, editor)
        self.assertEqual(len(editor["headline_candidates"]), 2)
        self.assertEqual(chief["annotations"][0]["questioned_annotation_id"], "editor-0")

        broken = valid_chief(structure)
        broken["annotations"][0]["questioned_annotation_id"] = "does-not-exist"
        warnings: list[str] = []
        normalized = validate_chief_output(
            broken,
            CONTENT,
            structure,
            editor,
            normalized_warnings=warnings,
        )
        self.assertIsNone(normalized["annotations"][0]["questioned_annotation_id"])
        self.assertTrue(any("questioned_annotation_id" in item for item in warnings))

    def test_unknown_fields_are_discarded_but_missing_function_fails(self) -> None:
        structure = detect_structure(CONTENT)
        output = valid_editor(structure)
        output["unexpected"] = True
        warnings: list[str] = []
        normalized = validate_editor_output(
            output, CONTENT, structure, normalized_warnings=warnings
        )
        self.assertNotIn("unexpected", normalized)
        self.assertTrue(any("未知欄位" in item for item in warnings))
        # 規則：缺某一類功能批註只記警示，不退件。
        output = valid_editor(structure)
        output["annotations"] = output["annotations"][:-1]
        warnings = []
        normalized = validate_editor_output(
            output, CONTENT, structure, normalized_warnings=warnings
        )
        self.assertEqual(len(normalized["annotations"]), len(output["annotations"]))
        self.assertTrue(any("沒有這些功能的批註" in item for item in warnings))

    def test_bad_internal_ids_are_normalized_and_unique(self) -> None:
        structure = detect_structure(CONTENT)
        output = valid_editor(structure)
        output["annotations"][0]["id"] = 'editor-0"]'
        output["annotations"][1]["id"] = "wrong-prefix"
        output["annotations"][2].pop("id")
        output["annotations"][3]["id"] = "editor-4"
        output["annotations"][4]["id"] = "editor-4"
        output["headline_candidates"][0]["id"] = "title-1"
        output["headline_candidates"][1].pop("id")

        editor = validate_editor_output(output, CONTENT, structure)
        editor_ids = [item["id"] for item in editor["annotations"]]
        headline_ids = [item["id"] for item in editor["headline_candidates"]]
        self.assertTrue(all(item.startswith("editor-") for item in editor_ids))
        self.assertEqual(len(editor_ids), len(set(editor_ids)))
        self.assertTrue(all(item.startswith("headline-") for item in headline_ids))
        self.assertEqual(len(headline_ids), len(set(headline_ids)))

        chief_output = valid_chief(structure)
        chief_output["annotations"][0]["id"] = "editor-1"
        chief_output["annotations"][0]["questioned_annotation_id"] = editor_ids[0]
        chief_output["annotations"][1].pop("id")
        chief_output["annotations"][2]["id"] = "chief-1"
        chief = validate_chief_output(chief_output, CONTENT, structure, editor)
        chief_ids = [item["id"] for item in chief["annotations"]]
        self.assertTrue(all(item.startswith("chief-") for item in chief_ids))
        self.assertEqual(len(chief_ids), len(set(chief_ids)))

    def test_content_problems_are_reported_not_rejected(self) -> None:
        """規則：設定不可過嚴；指出問題就好，該過還是要過。"""
        structure = detect_structure(CONTENT)

        # 沒有 message＝這一條沒內容，只丟掉這一條，其餘照過。
        output = valid_editor(structure)
        total = len(output["annotations"])
        output["annotations"][0].pop("message")
        warnings: list[str] = []
        editor = validate_editor_output(
            output, CONTENT, structure, normalized_warnings=warnings
        )
        self.assertEqual(len(editor["annotations"]), total - 1)
        self.assertTrue(any("沒有批註內容" in item for item in warnings))

        # 缺 function 或給了清單外的功能：內容保留，歸到最後一個功能並記警示。
        for bad_function in (None, "不存在的類別"):
            output = valid_editor(structure)
            total = len(output["annotations"])
            if bad_function is None:
                output["annotations"][0].pop("function")
            else:
                output["annotations"][0]["function"] = bad_function
            warnings = []
            editor = validate_editor_output(
                output, CONTENT, structure, normalized_warnings=warnings
            )
            with self.subTest(function=bad_function):
                self.assertEqual(len(editor["annotations"]), total)
                self.assertIn(editor["annotations"][0]["function"], EDITOR_FUNCTIONS)
                self.assertTrue(any("不在清單內" in item for item in warnings))

        # 空白 message 同樣視為沒內容，丟該條、不整批退件。
        for invalid_message in ("", "   "):
            output = valid_editor(structure)
            total = len(output["annotations"])
            output["annotations"][0]["message"] = invalid_message
            warnings = []
            editor = validate_editor_output(
                output, CONTENT, structure, normalized_warnings=warnings
            )
            with self.subTest(message=invalid_message):
                self.assertEqual(len(editor["annotations"]), total - 1)

        # 下標候選不足 2 組：記警示，照過。
        output = valid_editor(structure)
        output["headline_candidates"] = output["headline_candidates"][:1]
        warnings = []
        editor = validate_editor_output(
            output, CONTENT, structure, normalized_warnings=warnings
        )
        self.assertEqual(len(editor["headline_candidates"]), 1)
        self.assertTrue(any("少於建議" in item for item in warnings))

        output = valid_editor(structure)
        output["annotations"][0].pop("severity")
        warnings: list[str] = []
        editor = validate_editor_output(
            output, CONTENT, structure, normalized_warnings=warnings
        )
        self.assertEqual(editor["annotations"][0]["severity"], "note")
        self.assertTrue(any("severity" in item for item in warnings))

        output = valid_editor(structure)
        output["annotations"][0]["structure_block_id"] = "fabricated-block"
        warnings = []
        editor = validate_editor_output(
            output, CONTENT, structure, normalized_warnings=warnings
        )
        self.assertIsNone(editor["annotations"][0]["structure_block_id"])
        self.assertTrue(any("structure_block_id" in item for item in warnings))

    def test_editor_excerpt_is_reanchored_with_referenced_block_preference(self) -> None:
        structure = detect_structure(CONTENT)
        subheading = next(block for block in structure["blocks"] if block["text"] == "小標")
        output = valid_editor(structure)
        output["annotations"][0].update(
            {
                "excerpt": "標",
                "start": 0,
                "end": 1,
                "structure_block_id": subheading["id"],
            }
        )
        warnings: list[str] = []

        editor = validate_editor_output(
            output, CONTENT, structure, normalized_warnings=warnings
        )

        annotation = editor["annotations"][0]
        self.assertEqual((annotation["start"], annotation["end"]), (15, 16))
        self.assertEqual(CONTENT[annotation["start"] : annotation["end"]], "標")
        self.assertTrue(any("依 excerpt 重錨" in item for item in warnings))

    def test_excerpt_not_found_nulls_coordinates_and_keeps_only_valid_block(self) -> None:
        structure = detect_structure(CONTENT)
        block_id = structure["blocks"][-1]["id"]
        output = valid_editor(structure)
        output["annotations"][0].update(
            {"excerpt": "稿件中沒有這段", "start": -20, "end": 999}
        )
        warnings: list[str] = []

        editor = validate_editor_output(
            output, CONTENT, structure, normalized_warnings=warnings
        )

        annotation = editor["annotations"][0]
        self.assertEqual(annotation["structure_block_id"], block_id)
        self.assertIsNone(annotation["start"])
        self.assertIsNone(annotation["end"])
        self.assertTrue(any("找不到" in item for item in warnings))

        unanchored_output = valid_editor(structure)
        unanchored_output["annotations"][0].update(
            {
                "excerpt": "稿件中仍然沒有這段",
                "start": 3,
                "end": None,
                "structure_block_id": None,
            }
        )
        unanchored = validate_editor_output(unanchored_output, CONTENT, structure)
        unanchored_annotation = unanchored["annotations"][0]
        self.assertIsNone(unanchored_annotation["structure_block_id"])
        self.assertIsNone(unanchored_annotation["start"])
        self.assertIsNone(unanchored_annotation["end"])

    def test_missing_anchor_metadata_is_defaulted_and_recorded(self) -> None:
        structure = detect_structure(CONTENT)
        output = valid_editor(structure)
        for key in ("severity", "excerpt", "start", "end", "structure_block_id"):
            output["annotations"][0].pop(key)
        warnings: list[str] = []

        editor = validate_editor_output(
            output, CONTENT, structure, normalized_warnings=warnings
        )

        annotation = editor["annotations"][0]
        self.assertEqual(annotation["severity"], "note")
        self.assertEqual(annotation["excerpt"], "")
        self.assertIsNone(annotation["start"])
        self.assertIsNone(annotation["end"])
        self.assertIsNone(annotation["structure_block_id"])
        self.assertTrue(any("缺少錨定欄位" in item for item in warnings))

    def test_nonexistent_block_is_nulled_before_global_excerpt_reanchoring(self) -> None:
        structure = detect_structure(CONTENT)
        output = valid_editor(structure)
        output["annotations"][0].update(
            {
                "excerpt": "正文",
                "start": None,
                "end": None,
                "structure_block_id": "fabricated-block",
            }
        )
        warnings: list[str] = []

        editor = validate_editor_output(
            output, CONTENT, structure, normalized_warnings=warnings
        )

        annotation = editor["annotations"][0]
        self.assertIsNone(annotation["structure_block_id"])
        self.assertEqual((annotation["start"], annotation["end"]), (20, 22))
        self.assertGreaterEqual(len(warnings), 2)

    def test_chief_excerpt_uses_the_same_server_side_reanchoring(self) -> None:
        structure = detect_structure(CONTENT)
        editor = validate_editor_output(valid_editor(structure), CONTENT, structure)
        lead = next(block for block in structure["blocks"] if "前言" in block["text"])
        output = valid_chief(structure)
        output["annotations"][0].update(
            {
                "excerpt": "前言",
                "start": 0,
                "end": 1,
                "structure_block_id": lead["id"],
            }
        )
        warnings: list[str] = []

        chief = validate_chief_output(
            output,
            CONTENT,
            structure,
            editor,
            normalized_warnings=warnings,
        )

        annotation = chief["annotations"][0]
        self.assertEqual((annotation["start"], annotation["end"]), (9, 11))
        self.assertEqual(annotation["questioned_annotation_id"], "editor-0")
        self.assertTrue(any("依 excerpt 重錨" in item for item in warnings))

    def test_two_pass_adapter_uses_catalog_cards_and_utf8_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            structure = detect_structure(CONTENT)
            editor_output = valid_editor(structure)
            editor_output["annotations"][0].update(
                {"excerpt": "標", "start": 0, "end": 1}
            )
            chief_output = valid_chief(structure)
            chief_output["annotations"][0].update(
                {"excerpt": "前言", "start": 0, "end": 1}
            )
            editor_json = json.dumps(editor_output, ensure_ascii=False)
            chief_json = json.dumps(chief_output, ensure_ascii=False)
            script = root / "fake_engine.py"
            script.write_text(
                "import json, pathlib, sys\n"
                "payload=json.loads(sys.stdin.read())\n"
                f"editor={editor_json!r}\n"
                f"chief={chief_json!r}\n"
                "pathlib.Path(sys.argv[1]).write_text(editor if payload['role']=='editor' else chief, encoding='utf-8')\n",
                encoding="utf-8",
            )
            config = root / "engine.json"
            config.write_text(
                json.dumps(
                    {
                        "name": "fake",
                        "command": [sys.executable, str(script), "{output_path}"],
                        "timeout_seconds": 5,
                        "output": "file",
                        "network_disclosure": "測試引擎不連網",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            catalog = FakeCatalog()
            adapter = SemanticEngineAdapter(catalog, config, root, root / "tmp")
            result = adapter.run_review("測試", CONTENT, structure)
            self.assertEqual(result["workflow"]["status"], "complete")
            self.assertEqual(
                catalog.calls,
                [
                    ("editors", "editor.news"),
                    ("editors", "editor.baigui_editor_in_chief"),
                ],
            )
            self.assertIn("editor", result["provenance"]["input_hashes"])
            self.assertIn("chief", result["provenance"]["output_hashes"])
            for semantic_pass in result["provenance"]["passes"]:
                warning_record = semantic_pass["normalized_warnings"]
                self.assertEqual(warning_record["count"], len(warning_record["messages"]))
                self.assertGreater(warning_record["count"], 0)

    def test_two_pass_adapter_succeeds_with_bad_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            structure = detect_structure(CONTENT)
            editor_output = valid_editor(structure)
            editor_output["annotations"][0]["id"] = "annotation-1"
            editor_output["annotations"][1]["id"] = "annotation-1"
            editor_output["headline_candidates"][0]["id"] = "candidate-1"
            editor_output["headline_candidates"][1]["id"] = "candidate-1"
            chief_output = valid_chief(structure)
            chief_output["annotations"][0]["id"] = "annotation-1"
            chief_output["annotations"][0]["questioned_annotation_id"] = "editor-1"
            chief_output["annotations"][1]["id"] = "annotation-1"
            editor_json = json.dumps(editor_output, ensure_ascii=False)
            chief_json = json.dumps(chief_output, ensure_ascii=False)
            script = root / "bad_id_engine.py"
            script.write_text(
                "import json, pathlib, sys\n"
                "payload=json.loads(sys.stdin.read())\n"
                f"editor={editor_json!r}\n"
                f"chief={chief_json!r}\n"
                "pathlib.Path(sys.argv[1]).write_text(editor if payload['role']=='editor' else chief, encoding='utf-8')\n",
                encoding="utf-8",
            )
            config = root / "engine.json"
            config.write_text(
                json.dumps(
                    {
                        "name": "bad-id-fake",
                        "command": [sys.executable, str(script), "{output_path}"],
                        "timeout_seconds": 5,
                        "output": "file",
                        "network_disclosure": "測試引擎不連網",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = SemanticEngineAdapter(FakeCatalog(), config, root, root / "tmp").run_review(
                "測試", CONTENT, structure
            )

            self.assertEqual(result["workflow"]["status"], "complete")
            for key, prefix in (("editor", "editor-"), ("chief", "chief-")):
                ids = [item["id"] for item in result[key]["annotations"]]
                self.assertTrue(all(item.startswith(prefix) for item in ids))
                self.assertEqual(len(ids), len(set(ids)))
            headline_ids = [item["id"] for item in result["editor"]["headline_candidates"]]
            self.assertTrue(all(item.startswith("headline-") for item in headline_ids))
            self.assertEqual(len(headline_ids), len(set(headline_ids)))
            for semantic_pass in result["provenance"]["passes"]:
                self.assertGreater(semantic_pass["normalized_warnings"]["count"], 0)

    def test_missing_engine_config_becomes_failed_background_run_without_annotations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            repository = DocumentRepository(root / "studio.db")
            document = repository.create_document("測試", CONTENT)
            adapter = SemanticEngineAdapter(FakeCatalog(), root / "missing.json", root, root / "tmp")
            jobs = SemanticJobManager(repository, adapter)
            job = jobs.start(
                document["id"],
                document["current_revision_id"],
                document["title"],
                document["content"],
                detect_structure(CONTENT),
            )
            deadline = time.time() + 3
            result = jobs.get(job["id"])
            while result["status"] in {"queued", "running"} and time.time() < deadline:
                time.sleep(0.02)
                result = jobs.get(job["id"])
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["run"]["status"], "failed")
            self.assertEqual(result["run"]["output"]["summary"]["total"], 0)
            self.assertNotIn("editor", result["run"]["output"])

    def test_invalid_json_and_timeout_fail_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            structure = detect_structure(CONTENT)
            invalid_config = root / "invalid.json"
            invalid_config.write_text(
                json.dumps(
                    {
                        "name": "invalid",
                        "command": [sys.executable, "-c", "print('```json')"],
                        "timeout_seconds": 5,
                        "output": "stdout",
                        "network_disclosure": "測試引擎不連網",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            invalid = SemanticEngineAdapter(FakeCatalog(), invalid_config, root, root / "tmp")
            with self.assertRaises(SemanticFailure) as invalid_error:
                invalid.run_review("測試", CONTENT, structure)
            self.assertEqual(invalid_error.exception.code, "engine_output_invalid_json")

            timeout_config = root / "timeout.json"
            timeout_config.write_text(
                json.dumps(
                    {
                        "name": "slow",
                        "command": [sys.executable, "-c", "import time; time.sleep(3)"],
                        "timeout_seconds": 1,
                        "output": "stdout",
                        "network_disclosure": "測試引擎不連網",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            slow = SemanticEngineAdapter(FakeCatalog(), timeout_config, root, root / "tmp")
            with self.assertRaises(SemanticFailure) as timeout_error:
                slow.run_review("測試", CONTENT, structure)
            self.assertEqual(timeout_error.exception.code, "engine_timeout")


if __name__ == "__main__":
    unittest.main()
