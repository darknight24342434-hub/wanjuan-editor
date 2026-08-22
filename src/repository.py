from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class DocumentRepository:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    original_revision_id TEXT,
                    current_revision_id TEXT
                );

                CREATE TABLE IF NOT EXISTS revisions (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    parent_revision_id TEXT,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    engine TEXT NOT NULL,
                    card_id TEXT,
                    template_id TEXT,
                    persona_id TEXT,
                    input_hash TEXT NOT NULL,
                    output_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE,
                    FOREIGN KEY(revision_id) REFERENCES revisions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS source_decisions (
                    document_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    clue_id TEXT NOT NULL,
                    clue_text TEXT NOT NULL,
                    clue_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    note TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(revision_id, clue_id),
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE,
                    FOREIGN KEY(revision_id) REFERENCES revisions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS revision_source_hints (
                    revision_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    source_hint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE,
                    FOREIGN KEY(revision_id) REFERENCES revisions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL UNIQUE,
                    review_run_id TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    note TEXT NOT NULL,
                    source_snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE,
                    FOREIGN KEY(revision_id) REFERENCES revisions(id) ON DELETE CASCADE,
                    FOREIGN KEY(review_run_id) REFERENCES runs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_revisions_document
                    ON revisions(document_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_runs_document
                    ON runs(document_id, created_at DESC);
                """
            )
            db.commit()

    def ensure_welcome_document(self) -> None:
        if self.list_documents():
            return
        self.create_document(
            "歡迎使用萬卷文庫",
            "# 萬卷文庫\n\n這是一份可編輯的本機文件。\n\n"
            "左側選擇大師卡，中間編輯正文，右側選擇人物卡並執行審稿。\n\n"
            "原稿會保留，之後每次儲存都建立可追溯版本。",
        )

    def create_document(self, title: str, content: str = "") -> dict[str, Any]:
        document_id = str(uuid.uuid4())
        revision_id = str(uuid.uuid4())
        now = utc_now()
        clean_title = title.strip() or "未命名文件"
        digest = content_hash(content)
        with closing(self._connect()) as db:
            db.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?)",
                (document_id, clean_title, now, now, revision_id, revision_id),
            )
            db.execute(
                "INSERT INTO revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (revision_id, document_id, None, content, digest, "human", "建立原稿", now),
            )
            db.commit()
        return self.get_document(document_id)

    def list_documents(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute(
                """
                SELECT d.*, r.content_hash, length(r.content) AS content_length
                FROM documents d
                JOIN revisions r ON r.id = d.current_revision_id
                ORDER BY d.updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_document(self, document_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute(
                """
                SELECT d.*, r.content, r.content_hash, r.actor, r.note,
                       original.content_hash AS original_hash
                FROM documents d
                JOIN revisions r ON r.id = d.current_revision_id
                JOIN revisions original ON original.id = d.original_revision_id
                WHERE d.id = ?
                """,
                (document_id,),
            ).fetchone()
        if not row:
            raise KeyError("找不到文件")
        return dict(row)

    def save_revision(
        self,
        document_id: str,
        title: str,
        content: str,
        actor: str = "human",
        note: str = "儲存版本",
        force: bool = False,
        expected_current_revision_id: str | None = None,
    ) -> dict[str, Any]:
        current = self.get_document(document_id)
        if (
            expected_current_revision_id is not None
            and current["current_revision_id"] != expected_current_revision_id
        ):
            raise ValueError("稿件版本已更新，背景結果不會覆蓋目前版本")
        clean_title = title.strip() or "未命名文件"
        if not force and current["content"] == content and current["title"] == clean_title:
            return current
        revision_id = str(uuid.uuid4())
        now = utc_now()
        digest = content_hash(content)
        with closing(self._connect()) as db:
            if expected_current_revision_id is not None:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT current_revision_id FROM documents WHERE id = ?",
                    (document_id,),
                ).fetchone()
                if not row or row["current_revision_id"] != expected_current_revision_id:
                    raise ValueError("稿件版本已更新，背景結果不會覆蓋目前版本")
            db.execute(
                "INSERT INTO revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    document_id,
                    current["current_revision_id"],
                    content,
                    digest,
                    actor,
                    note,
                    now,
                ),
            )
            db.execute(
                "UPDATE documents SET title = ?, updated_at = ?, current_revision_id = ? WHERE id = ?",
                (clean_title, now, revision_id, document_id),
            )
            db.commit()
        return self.get_document(document_id)

    def save_source_hint(self, document_id: str, revision_id: str, source_hint: str) -> None:
        self.get_revision(document_id, revision_id)
        clean = source_hint.strip()[:500]
        if not clean:
            with closing(self._connect()) as db:
                db.execute(
                    "DELETE FROM revision_source_hints WHERE document_id = ? AND revision_id = ?",
                    (document_id, revision_id),
                )
                db.commit()
            return
        with closing(self._connect()) as db:
            db.execute(
                """
                INSERT INTO revision_source_hints (revision_id, document_id, source_hint, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(revision_id) DO UPDATE SET source_hint = excluded.source_hint
                """,
                (revision_id, document_id, clean, utc_now()),
            )
            db.commit()

    def get_source_hint(self, document_id: str, revision_id: str) -> str:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT source_hint FROM revision_source_hints WHERE document_id = ? AND revision_id = ?",
                (document_id, revision_id),
            ).fetchone()
        return str(row["source_hint"]) if row else ""

    def list_source_decisions(self, document_id: str, revision_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM source_decisions WHERE document_id = ? AND revision_id = ? ORDER BY rowid",
                (document_id, revision_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_source_decisions(
        self,
        document_id: str,
        revision_id: str,
        decisions: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        self.get_revision(document_id, revision_id)
        now = utc_now()
        with closing(self._connect()) as db:
            for item in decisions:
                db.execute(
                    """
                    INSERT INTO source_decisions
                        (document_id, revision_id, clue_id, clue_text, clue_kind, status, note, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(revision_id, clue_id) DO UPDATE SET
                        clue_text = excluded.clue_text,
                        clue_kind = excluded.clue_kind,
                        status = excluded.status,
                        note = excluded.note,
                        updated_at = excluded.updated_at
                    """,
                    (
                        document_id,
                        revision_id,
                        item["clue_id"],
                        item["clue_text"],
                        item["clue_kind"],
                        item["status"],
                        item.get("note", ""),
                        now,
                    ),
                )
            db.commit()
        return self.list_source_decisions(document_id, revision_id)

    def copy_source_decisions(self, document_id: str, from_revision_id: str, to_revision_id: str) -> None:
        decisions = self.list_source_decisions(document_id, from_revision_id)
        if not decisions:
            return
        self.save_source_decisions(
            document_id,
            to_revision_id,
            [
                {
                    "clue_id": item["clue_id"],
                    "clue_text": item["clue_text"],
                    "clue_kind": item["clue_kind"],
                    "status": item["status"],
                    "note": item["note"],
                }
                for item in decisions
            ],
        )

    def record_approval(
        self,
        document_id: str,
        revision_id: str,
        review_run_id: str,
        source_snapshot: list[dict[str, Any]],
        reviewer: str = "human",
        note: str = "人工核准目前版本",
    ) -> dict[str, Any]:
        self.get_revision(document_id, revision_id)
        review_run = self.get_run(review_run_id)
        if review_run["document_id"] != document_id or review_run["revision_id"] != revision_id:
            raise ValueError("核准紀錄與目前版本不一致")
        approval_id = str(uuid.uuid4())
        now = utc_now()
        with closing(self._connect()) as db:
            db.execute(
                """
                INSERT INTO approvals
                    (id, document_id, revision_id, review_run_id, reviewer, note, source_snapshot_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(revision_id) DO UPDATE SET
                    review_run_id = excluded.review_run_id,
                    reviewer = excluded.reviewer,
                    note = excluded.note,
                    source_snapshot_json = excluded.source_snapshot_json,
                    created_at = excluded.created_at
                """,
                (
                    approval_id,
                    document_id,
                    revision_id,
                    review_run_id,
                    reviewer,
                    note,
                    json.dumps(source_snapshot, ensure_ascii=False),
                    now,
                ),
            )
            db.commit()
        return self.get_approval(document_id, revision_id)

    def get_approval(self, document_id: str, revision_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM approvals WHERE document_id = ? AND revision_id = ?",
                (document_id, revision_id),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["source_snapshot"] = json.loads(result.pop("source_snapshot_json"))
        return result

    def list_revisions(self, document_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute(
                """
                SELECT r.*, CASE WHEN r.id = d.original_revision_id THEN 1 ELSE 0 END AS is_original,
                       CASE WHEN r.id = d.current_revision_id THEN 1 ELSE 0 END AS is_current
                FROM revisions r
                JOIN documents d ON d.id = r.document_id
                WHERE r.document_id = ?
                ORDER BY r.created_at DESC, r.rowid DESC
                """,
                (document_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_revision(self, document_id: str, revision_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM revisions WHERE document_id = ? AND id = ?",
                (document_id, revision_id),
            ).fetchone()
        if not row:
            raise KeyError("找不到版本")
        return dict(row)

    def restore_revision(self, document_id: str, revision_id: str) -> dict[str, Any]:
        revision = self.get_revision(document_id, revision_id)
        document = self.get_document(document_id)
        source_hint = self.get_source_hint(document_id, revision_id)
        restored = self.save_revision(
            document_id,
            document["title"],
            revision["content"],
            actor="restore",
            note=f"還原自版本 {revision_id[:8]}",
        )
        self.save_source_hint(document_id, restored["current_revision_id"], source_hint)
        return restored

    def record_run(
        self,
        document_id: str,
        revision_id: str,
        action: str,
        output: dict[str, Any],
        card_id: str | None = None,
        template_id: str | None = None,
        persona_id: str | None = None,
        status: str = "complete",
        engine: str = "local_rules",
    ) -> dict[str, Any]:
        revision = self.get_revision(document_id, revision_id)
        run_id = str(uuid.uuid4())
        now = utc_now()
        with closing(self._connect()) as db:
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    document_id,
                    revision_id,
                    action,
                    status,
                    engine,
                    card_id,
                    template_id,
                    persona_id,
                    revision["content_hash"],
                    json.dumps(output, ensure_ascii=False),
                    now,
                    now if status == "complete" else None,
                ),
            )
            db.commit()
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise KeyError("找不到執行紀錄")
        result = dict(row)
        result["output"] = json.loads(result.pop("output_json"))
        return result

    def list_runs(self, document_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM runs WHERE document_id = ? ORDER BY created_at DESC, rowid DESC",
                (document_id,),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["output"] = json.loads(item.pop("output_json"))
            results.append(item)
        return results
