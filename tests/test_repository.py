from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.repository import DocumentRepository, content_hash


class RepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repository = DocumentRepository(Path(self.temp.name) / "studio.db")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_original_revision_remains_immutable(self) -> None:
        created = self.repository.create_document("原稿", "第一版")
        original_hash = created["original_hash"]
        updated = self.repository.save_revision(created["id"], "原稿", "第二版")
        self.assertEqual(updated["original_hash"], original_hash)
        self.assertEqual(original_hash, content_hash("第一版"))
        self.assertEqual(updated["content_hash"], content_hash("第二版"))
        revisions = self.repository.list_revisions(created["id"])
        self.assertEqual(len(revisions), 2)
        self.assertEqual(sum(row["is_original"] for row in revisions), 1)

    def test_same_content_does_not_create_duplicate_revision(self) -> None:
        created = self.repository.create_document("原稿", "內容")
        self.repository.save_revision(created["id"], "原稿", "內容")
        self.assertEqual(len(self.repository.list_revisions(created["id"])), 1)

    def test_restore_creates_new_revision(self) -> None:
        created = self.repository.create_document("原稿", "第一版")
        self.repository.save_revision(created["id"], "原稿", "第二版")
        original = next(row for row in self.repository.list_revisions(created["id"]) if row["is_original"])
        restored = self.repository.restore_revision(created["id"], original["id"])
        self.assertEqual(restored["content"], "第一版")
        self.assertEqual(len(self.repository.list_revisions(created["id"])), 3)

    def test_source_decisions_copy_and_revision_bound_approval(self) -> None:
        created = self.repository.create_document("新聞", "市府表示將公告。")
        revision_id = created["current_revision_id"]
        decisions = self.repository.save_source_decisions(
            created["id"],
            revision_id,
            [
                {
                    "clue_id": "source-1",
                    "clue_text": "市府表示將公告。",
                    "clue_kind": "attribution",
                    "status": "confirmed",
                    "note": "",
                }
            ],
        )
        rewritten = self.repository.save_revision(
            created["id"],
            created["title"],
            created["content"],
            actor="persona:editor.news",
            note="新聞編輯卡建立修訂版本",
            force=True,
        )
        self.repository.copy_source_decisions(created["id"], revision_id, rewritten["current_revision_id"])
        copied = self.repository.list_source_decisions(created["id"], rewritten["current_revision_id"])
        self.assertEqual(copied[0]["status"], "confirmed")

        run = self.repository.record_run(
            created["id"],
            rewritten["current_revision_id"],
            "local_review",
            {"workflow": {"id": "chinatimes_newsroom"}, "summary": {"total": 0}},
        )
        approval = self.repository.record_approval(
            created["id"],
            rewritten["current_revision_id"],
            run["id"],
            decisions,
        )
        self.assertEqual(approval["revision_id"], rewritten["current_revision_id"])
        self.assertEqual(len(approval["source_snapshot"]), 1)

    def test_attachment_source_hint_requires_explicit_carry_and_can_clear(self) -> None:
        created = self.repository.create_document("新聞", "來源：測試附件")
        self.repository.save_source_hint(
            created["id"],
            created["current_revision_id"],
            "附件檔名：採訪紀錄.docx",
        )
        updated = self.repository.save_revision(
            created["id"],
            created["title"],
            created["content"] + "\n補充",
        )
        self.assertEqual(
            self.repository.get_source_hint(created["id"], updated["current_revision_id"]),
            "",
        )
        self.repository.save_source_hint(
            created["id"],
            updated["current_revision_id"],
            "附件檔名：採訪紀錄.docx",
        )
        self.assertEqual(
            self.repository.get_source_hint(created["id"], updated["current_revision_id"]),
            "附件檔名：採訪紀錄.docx",
        )
        self.repository.save_source_hint(created["id"], updated["current_revision_id"], "")
        self.assertEqual(
            self.repository.get_source_hint(created["id"], updated["current_revision_id"]),
            "",
        )


if __name__ == "__main__":
    unittest.main()
