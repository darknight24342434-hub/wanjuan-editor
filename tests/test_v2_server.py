from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from src.newsroom import extract_source_clues
from src.server import AppContext, build_server


class V2ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        catalog_root = root / "catalog"
        (catalog_root / "editors").mkdir(parents=True)
        for asset_id, filename in (
            ("editor.news", "news.md"),
            ("editor.baigui_editor_in_chief", "chief.md"),
        ):
            (catalog_root / "editors" / filename).write_text(
                f"---\nid: {asset_id}\nname: 測試人物卡\nstatus: active\n---\n# 測試人物卡\n",
                encoding="utf-8",
            )
        (catalog_root / "INDEX.yaml").write_text(
            "updated: '2026-08-13'\nactive_assets:\n"
            "  style_cards: []\n  templates: []\n  editors:\n"
            "    - id: editor.news\n      name: 新聞編輯卡\n      path: editors/news.md\n      status: active\n"
            "    - id: editor.baigui_editor_in_chief\n      name: 百鬼總編卡\n      path: editors/chief.md\n      status: active\n",
            encoding="utf-8",
        )
        self.context = AppContext(
            catalog_root,
            root / "studio.db",
            root,
            root / "missing-engine.json",
        )
        self.server = build_server("127.0.0.1", 0, self.context)
        self.port = int(self.server.server_address[1])
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Origin": self.base,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def test_structure_stale_tab_background_failure_and_title_revision(self) -> None:
        status, document = self.request(
            "/api/documents",
            "POST",
            {"title": "原標題", "content": "主標\n副標\n\n這是前言。\n\n小標\n\n來源：市府公告。"},
        )
        self.assertEqual(status, 201)
        document_id = document["id"]
        revision_id = document["current_revision_id"]

        status, structure = self.request(f"/api/documents/{document_id}/structure")
        self.assertEqual(status, 200)
        first = structure["blocks"][0]
        status, error = self.request(
            f"/api/documents/{document_id}/structure",
            "POST",
            {"expected_revision_id": "stale", "overrides": [{"id": first["id"], "type": "body"}]},
        )
        self.assertEqual(status, 409)
        self.assertIn("版本已更新", error["error"])

        status, structure = self.request(
            f"/api/documents/{document_id}/structure",
            "POST",
            {"expected_revision_id": revision_id, "overrides": [{"id": first["id"], "type": "body"}]},
        )
        self.assertEqual(status, 201)
        self.assertEqual(structure["blocks"][0]["type"], "body")

        status, _ = self.request(
            f"/api/documents/{document_id}/rewrite",
            "POST",
            {"skip_writer": True, "expected_revision_id": revision_id},
        )
        self.assertEqual(status, 201)

        started = time.perf_counter()
        status, job = self.request(
            f"/api/documents/{document_id}/semantic-review",
            "POST",
            {
                "expected_revision_id": revision_id,
                "expected_structure_hash": structure["structure_hash"],
            },
        )
        self.assertEqual(status, 202)
        self.assertLess(time.perf_counter() - started, 1.0)
        deadline = time.time() + 3
        while job["status"] in {"queued", "running"} and time.time() < deadline:
            time.sleep(0.03)
            _, job = self.request(f"/api/semantic/jobs/{job['id']}")
        self.assertEqual(job["status"], "failed")
        self.assertNotIn("editor", job["run"]["output"])
        self.assertEqual(
            job["run"]["output"]["provenance"]["structure_hash"],
            structure["structure_hash"],
        )
        status, preflight = self.request(
            f"/api/documents/{document_id}/preflight",
            "POST",
            {"expected_revision_id": revision_id, "source_notes": "來源：市府公告"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(preflight["engine"], "local_rules")

        clues = extract_source_clues(document["content"])
        self.context.repository.save_source_decisions(
            document_id,
            revision_id,
            [{**item, "status": "confirmed", "note": ""} for item in clues],
        )
        self.context.repository.save_source_hint(document_id, revision_id, "附件：採訪紀錄")
        semantic = self.context.repository.record_run(
            document_id,
            revision_id,
            "semantic_review",
            {
                "editor": {
                    "headline_candidates": [
                        {"id": "candidate-1", "main_title": "採用後標題", "subtitle": "副標", "angle": "進度"}
                    ]
                },
                "provenance": {"structure_hash": structure["structure_hash"]},
                "summary": {"total": 1},
            },
            status="complete",
            engine="fake",
        )
        status, payload = self.request(
            f"/api/documents/{document_id}/semantic-title",
            "POST",
            {
                "expected_revision_id": revision_id,
                "semantic_run_id": semantic["id"],
                "candidate_id": "candidate-1",
            },
        )
        self.assertEqual(status, 201)
        updated = payload["document"]
        self.assertEqual(updated["title"], "採用後標題")
        self.assertNotEqual(updated["current_revision_id"], revision_id)
        self.assertEqual(updated["original_hash"], document["original_hash"])
        self.assertEqual(len(self.context.repository.list_revisions(document_id)), 2)
        self.assertEqual(
            self.context.repository.get_source_hint(document_id, updated["current_revision_id"]),
            "附件：採訪紀錄",
        )
        self.assertEqual(
            self.context.repository.list_source_decisions(document_id, updated["current_revision_id"])[0]["status"],
            "confirmed",
        )
        self.assertEqual(payload["structure"]["blocks"][0]["type"], "body")

    def test_writer_registry_skip_gate_and_stale_rewrite_request(self) -> None:
        concept = urllib.parse.quote("人物訪談逐字稿")
        status, cards = self.request(f"/api/writer/cards?concept={concept}")
        self.assertEqual(status, 200)
        self.assertEqual(cards["default_id"], "writer.dong_chengyu")
        # Three shipped writer cards. A fourth, a personal style card pointing into a
        # private persona project, was removed before this repository was published.
        self.assertEqual(len(cards["items"]), 3)
        self.assertTrue(next(item for item in cards["items"] if item["id"] == "writer.dong_chengyu")["default"])

        status, document = self.request(
            "/api/documents", "POST", {"title": "訪談", "content": "受訪者表示這是逐字稿。"}
        )
        self.assertEqual(status, 201)
        document_id = document["id"]
        revision_id = document["current_revision_id"]

        status, error = self.request(
            f"/api/documents/{document_id}/review",
            "POST",
            {
                "workflow_id": "chinatimes_newsroom",
                "persona_id": "editor.news",
                "chief_persona_id": "editor.baigui_editor_in_chief",
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("寫手尚未完成", error["error"])

        status, error = self.request(
            f"/api/documents/{document_id}/semantic-review",
            "POST",
            {"expected_revision_id": revision_id},
        )
        self.assertEqual(status, 400)
        self.assertIn("寫手尚未完成", error["error"])

        status, error = self.request(
            f"/api/documents/{document_id}/rewrite",
            "POST",
            {
                "writer_card_id": "writer.dong_chengyu",
                "target_length": 2000,
                "expected_revision_id": "stale",
            },
        )
        self.assertEqual(status, 409)

        status, skipped = self.request(
            f"/api/documents/{document_id}/rewrite",
            "POST",
            {"skip_writer": True, "expected_revision_id": revision_id},
        )
        self.assertEqual(status, 201)
        self.assertTrue(skipped["workflow"]["writer"]["allowed_to_edit"])
        self.assertTrue(skipped["workflow"]["writer"]["skipped"])
        self.assertFalse(skipped["workflow"]["rewritten"])
        self.assertTrue(skipped["workflow"]["finalizable"])


if __name__ == "__main__":
    unittest.main()
