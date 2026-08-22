from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.repository import DocumentRepository
from src.semantic import SemanticEngineAdapter, SemanticFailure
from src.structure import detect_structure
from src.server import newsroom_state
from src.writer import (
    WriterCardRegistry,
    WriterEngineAdapter,
    WriterFailure,
    WriterJobManager,
    normalize_writer_output,
)


class FakeWriterAdapter(WriterEngineAdapter):
    def __init__(self, outputs: dict[str, object]):
        super().__init__(Path.cwd())
        self.outputs = outputs
        self.calls: list[str] = []

    def _invoke(self, engine: str, prompt: str):
        self.calls.append(engine)
        value = self.outputs[engine]
        if isinstance(value, Exception):
            raise value
        return str(value), {"engine": engine, "elapsed_ms": 1, "raw_output_hash": f"hash-{engine}"}


class WriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.library = self.root / "library"
        (self.library / "cards").mkdir(parents=True)
        (self.library / "cards" / "relative.md").write_text("相對卡片", encoding="utf-8")
        self.external = self.root / "external.md"
        self.external.write_text("外部卡片", encoding="utf-8")
        self.config = self.root / "writer_cards.json"
        self.config.write_text(
            json.dumps(
                {
                    "default_id": "writer.default",
                    "cards": [
                        {
                            "id": "writer.default",
                            "name": "預設卡",
                            "category": "人物訪談（建議）",
                            "path": str(self.external),
                            "engine": "claude_cli",
                            "fallback_engine": "agy_cli",
                        },
                        {
                            "id": "writer.relative",
                            "name": "相對卡",
                            "category": "財經事件",
                            "path": "cards/relative.md",
                            "engine": "agy_cli",
                            "fallback_engine": "claude_cli",
                        },
                        {
                            "id": "writer.missing",
                            "name": "失聯卡",
                            "category": "汽車",
                            "path": "cards/missing.md",
                            "engine": "agy_cli",
                            "fallback_engine": "claude_cli",
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.registry = WriterCardRegistry(self.config, self.library)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def valid_output(draft: str = "這是一篇可讀的改寫稿。") -> str:
        return (
            f"<<<DRAFT>>>\n{draft}\n<<<REPORT_JSON>>>\n"
            '{"speaker_assessment":["講者甲為企業代表"],"length_note":"素材不足，未灌水",'
            '"gaps":["日期待補"],"names_to_verify":["原樣名；疑為正名；待查證"]}'
        )

    def wait_job(self, manager: WriterJobManager, job_id: str) -> dict:
        deadline = time.time() + 3
        job = manager.get(job_id)
        while job["status"] in {"queued", "running"} and time.time() < deadline:
            time.sleep(0.01)
            job = manager.get(job_id)
        return job

    def test_registry_resolves_relative_and_absolute_and_keeps_missing_visible(self) -> None:
        self.assertEqual(self.registry.snapshot("writer.default")["content"], "外部卡片")
        self.assertEqual(self.registry.snapshot("writer.relative")["content"], "相對卡片")
        cards = self.registry.list_cards("汽車新車試駕")
        self.assertEqual(len(cards), 3)
        self.assertTrue(next(item for item in cards if item["id"] == "writer.default")["default"])
        missing = next(item for item in cards if item["id"] == "writer.missing")
        self.assertFalse(missing["available"])
        self.assertEqual(missing["source_label"], "卡源失聯")

    def test_normalization_keeps_draft_and_warns_on_metadata(self) -> None:
        draft, report, warnings = normalize_writer_output(
            '<<<DRAFT>>>\n可用正文。\n<<<REPORT>>>\n{"speaker_assessment":"甲方代表","extra":1}'
        )
        self.assertEqual(draft, "可用正文。")
        self.assertEqual(report["speaker_assessment"], ["甲方代表"])
        self.assertTrue(any("未知欄位" in item for item in warnings))
        self.assertTrue(any("gaps" in item for item in warnings))

    def test_empty_output_is_honest_failure(self) -> None:
        with self.assertRaises(WriterFailure) as caught:
            normalize_writer_output(" \n ")
        self.assertEqual(caught.exception.code, "writer_output_unusable")

    def test_primary_failure_uses_fallback_and_labels_proxy(self) -> None:
        adapter = FakeWriterAdapter(
            {
                "claude_cli": WriterFailure("writer_engine_failed", "主席故障"),
                "agy_cli": self.valid_output(),
            }
        )
        result = adapter.run_rewrite("標題", "逐字稿", self.registry.snapshot("writer.default"), 2000)
        self.assertEqual(adapter.calls, ["claude_cli", "agy_cli"])
        self.assertTrue(result["provenance"]["proxy"])
        self.assertEqual(result["provenance"]["proxy_label"], "本稿由備位引擎代打")

    def test_both_engines_fail_without_fabricated_draft(self) -> None:
        adapter = FakeWriterAdapter(
            {
                "claude_cli": WriterFailure("writer_engine_failed", "主席故障"),
                "agy_cli": WriterFailure("writer_engine_failed", "備位故障"),
            }
        )
        with self.assertRaises(WriterFailure) as caught:
            adapter.run_rewrite("標題", "逐字稿", self.registry.snapshot("writer.default"), 2000)
        self.assertEqual(caught.exception.code, "writer_all_engines_failed")

    def test_job_creates_revision_preserves_original_hash_and_provenance(self) -> None:
        repository = DocumentRepository(self.root / "studio.db")
        original = repository.create_document("訪談", "原始逐字稿")
        adapter = FakeWriterAdapter({"claude_cli": self.valid_output("整理後新聞稿。"), "agy_cli": ""})
        manager = WriterJobManager(repository, self.registry, adapter)
        job = manager.start(
            original["id"], original["current_revision_id"], original["title"], original["content"], "writer.default", 2000
        )
        finished = self.wait_job(manager, job["id"])
        self.assertEqual(finished["status"], "complete")
        current = repository.get_document(original["id"])
        self.assertEqual(current["content"], "整理後新聞稿。")
        self.assertEqual(current["original_hash"], original["original_hash"])
        self.assertEqual(len(repository.list_revisions(original["id"])), 2)
        run = finished["run"]
        self.assertEqual(run["card_id"], "writer.default")
        self.assertEqual(run["output"]["provenance"]["actual_engine"], "claude_cli")
        self.assertTrue(run["output"]["provenance"]["card"]["sha256"])
        self.assertNotIn("draft", run["output"])

    def test_stale_background_result_does_not_change_newer_revision(self) -> None:
        repository = DocumentRepository(self.root / "stale.db")
        original = repository.create_document("訪談", "原始逐字稿")

        class StaleAdapter(FakeWriterAdapter):
            def run_rewrite(inner_self, *args, **kwargs):
                repository.save_revision(original["id"], "訪談", "人工更新", force=True)
                return super(StaleAdapter, inner_self).run_rewrite(*args, **kwargs)

        adapter = StaleAdapter({"claude_cli": self.valid_output("過期結果"), "agy_cli": ""})
        manager = WriterJobManager(repository, self.registry, adapter)
        job = manager.start(
            original["id"], original["current_revision_id"], original["title"], original["content"], "writer.default", 2000
        )
        finished = self.wait_job(manager, job["id"])
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["error"]["code"], "writer_revision_stale")
        self.assertEqual(repository.get_document(original["id"])["content"], "人工更新")

    def test_semantic_rejects_same_writer_engine_without_different_fallback(self) -> None:
        engine = self.root / "engine.json"
        engine.write_text(
            json.dumps(
                {
                    "name": "claude_cli",
                    "command": ["not-used"],
                    "timeout_seconds": 10,
                    "output": "stdout",
                    "network_disclosure": "測試",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        class Catalog:
            def get_asset(self, kind, asset_id):
                return {"id": asset_id, "sha256": "sha", "content": "card"}

        adapter = SemanticEngineAdapter(Catalog(), engine, self.root, self.root / "tmp")
        with self.assertRaises(SemanticFailure) as caught:
            adapter.run_review("標題", "正文", detect_structure("正文"), writer_actual_engine="claude_cli")
        self.assertEqual(caught.exception.code, "editor_model_conflict")

    def test_writer_gate_and_engine_follow_human_revision_lineage(self) -> None:
        repository = DocumentRepository(self.root / "lineage.db")
        original = repository.create_document("訪談", "原稿")
        writer_revision = repository.save_revision(
            original["id"], "訪談", "寫手稿", actor="writer:writer.default", force=True
        )
        repository.record_run(
            original["id"],
            writer_revision["current_revision_id"],
            "writer_rewrite",
            {
                "report": {},
                "provenance": {"actual_engine": "claude_cli", "proxy": False},
                "summary": {"total": 0},
            },
            card_id="writer.default",
            status="complete",
            engine="claude_cli",
        )
        repository.save_revision(original["id"], "訪談", "主板人工微調", actor="human", force=True)
        state = newsroom_state(SimpleNamespace(repository=repository), original["id"])
        self.assertTrue(state["writer"]["allowed_to_edit"])
        self.assertEqual(state["writer"]["actual_engine"], "claude_cli")
        self.assertTrue(state["rewritten"])


if __name__ == "__main__":
    unittest.main()
