from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.newsroom import extract_source_clues
from src.repository import DocumentRepository
from src.server import newsroom_state


class WorkflowStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repository = DocumentRepository(Path(self.temp.name) / "studio.db")
        self.context = SimpleNamespace(repository=self.repository)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _approved_revision(self) -> tuple[dict, dict]:
        created = self.repository.create_document("新聞", "市府表示將公告。\n來源：市府公告")
        clues = extract_source_clues(created["content"])
        decisions = [{**item, "status": "confirmed", "note": ""} for item in clues]
        self.repository.save_source_decisions(
            created["id"],
            created["current_revision_id"],
            decisions,
        )
        reviewed = self.repository.record_run(
            created["id"],
            created["current_revision_id"],
            "local_review",
            {"workflow": {"id": "chinatimes_newsroom"}, "summary": {"total": 0}},
        )
        rewritten = self.repository.save_revision(
            created["id"],
            created["title"],
            created["content"],
            actor="persona:editor.news",
            note="新聞編輯卡建立修訂版本",
            force=True,
        )
        self.repository.copy_source_decisions(
            created["id"],
            created["current_revision_id"],
            rewritten["current_revision_id"],
        )
        reviewed = self.repository.record_run(
            created["id"],
            rewritten["current_revision_id"],
            "local_review",
            {"workflow": {"id": "chinatimes_newsroom"}, "summary": {"total": 0}},
        )
        current_sources = newsroom_state(self.context, created["id"])["sources"]
        self.repository.record_approval(
            created["id"],
            rewritten["current_revision_id"],
            reviewed["id"],
            current_sources,
        )
        return rewritten, reviewed

    def test_source_change_invalidates_approval(self) -> None:
        document, _ = self._approved_revision()
        state = newsroom_state(self.context, document["id"])
        self.assertTrue(state["approved"])
        changed = [dict(item) for item in state["sources"]]
        changed[0]["status"] = "doubt"
        changed[0]["note"] = "等待第二來源"
        self.repository.save_source_decisions(
            document["id"],
            document["current_revision_id"],
            changed,
        )
        self.assertFalse(newsroom_state(self.context, document["id"])["approved"])

    def test_new_review_invalidates_old_approval(self) -> None:
        document, _ = self._approved_revision()
        self.repository.record_run(
            document["id"],
            document["current_revision_id"],
            "local_review",
            {"workflow": {"id": "chinatimes_newsroom"}, "summary": {"total": 0}},
        )
        state = newsroom_state(self.context, document["id"])
        self.assertFalse(state["approved"])
        self.assertTrue(state["approval_stale"])

    def test_general_review_does_not_break_or_replace_newsroom_review(self) -> None:
        created = self.repository.create_document("混合模式", "市府表示將公告。")
        clues = extract_source_clues(created["content"])
        self.repository.save_source_decisions(
            created["id"],
            created["current_revision_id"],
            [{**item, "status": "confirmed", "note": ""} for item in clues],
        )
        self.repository.record_run(
            created["id"],
            created["current_revision_id"],
            "local_review",
            {"workflow": None, "summary": {"total": 0}},
        )
        state = newsroom_state(self.context, created["id"])
        self.assertFalse(state["review_current"])
        news = self.repository.record_run(
            created["id"],
            created["current_revision_id"],
            "local_review",
            {"workflow": {"id": "chinatimes_newsroom"}, "summary": {"total": 0}},
        )
        state = newsroom_state(self.context, created["id"])
        self.assertTrue(state["review_current"])
        self.assertEqual(state["review_run_id"], news["id"])


if __name__ == "__main__":
    unittest.main()
