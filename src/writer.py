from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .repository import DocumentRepository
from .semantic import json_hash, utc_now


WRITER_CONTRACT_VERSION = "newsroom_writer_v1"
ENGINE_COMMANDS = {
    "claude_cli": lambda prompt: ["claude", "-p", prompt, "--output-format", "text"],
    "claude_opus_cli": lambda prompt: ["claude", "-p", prompt, "--model", "opus", "--output-format", "text"],
    "agy_cli": lambda prompt: ["agy", "-p", prompt],
}


class WriterFailure(RuntimeError):
    def __init__(self, code: str, message: str, provenance: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.provenance = provenance or {}


class WriterCardRegistry:
    """Read the fixed writer registry while keeping every card source read-only."""

    def __init__(self, config_path: Path, library_root: Path):
        self.config_path = config_path.resolve()
        self.library_root = library_root.resolve()
        self.default_id = ""
        self._cards: list[dict[str, str]] = []
        self.reload()

    def reload(self) -> None:
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WriterFailure("writer_registry_invalid", f"寫手卡登記表無法讀取：{exc}") from exc
        cards = raw.get("cards") if isinstance(raw, dict) else None
        if not isinstance(cards, list) or not cards:
            raise WriterFailure("writer_registry_invalid", "寫手卡登記表缺少 cards")
        required = {"id", "name", "category", "path", "engine", "fallback_engine"}
        normalized: list[dict[str, str]] = []
        seen: set[str] = set()
        for index, item in enumerate(cards):
            if not isinstance(item, dict) or not required.issubset(item):
                raise WriterFailure("writer_registry_invalid", f"寫手卡第 {index + 1} 列欄位不完整")
            card = {key: str(item[key]) for key in required}
            if card["id"] in seen:
                raise WriterFailure("writer_registry_invalid", f"寫手卡 ID 重複：{card['id']}")
            if card["engine"] not in ENGINE_COMMANDS or card["fallback_engine"] not in ENGINE_COMMANDS:
                raise WriterFailure("writer_registry_invalid", f"寫手卡引擎不支援：{card['id']}")
            if card["engine"] == card["fallback_engine"]:
                raise WriterFailure("writer_registry_invalid", f"寫手卡主引擎與備位引擎不可相同：{card['id']}")
            seen.add(card["id"])
            normalized.append(card)
        default_id = str(raw.get("default_id") or "")
        if default_id not in seen:
            raise WriterFailure("writer_registry_invalid", "預設寫手卡不存在")
        self.default_id = default_id
        self._cards = normalized

    def _source_path(self, card: dict[str, str]) -> Path:
        raw = Path(card["path"])
        if raw.is_absolute():
            candidate = raw.resolve()
            if "00_admin" in candidate.parts:
                raise WriterFailure("writer_card_path_invalid", f"寫手卡不可讀取非 runtime 資產：{card['id']}")
            return candidate
        candidate = (self.library_root / raw).resolve()
        try:
            candidate.relative_to(self.library_root)
        except ValueError as exc:
            raise WriterFailure("writer_card_path_invalid", f"寫手卡路徑越界：{card['id']}") from exc
        if "00_admin" in candidate.parts:
            raise WriterFailure("writer_card_path_invalid", f"寫手卡不可讀取非 runtime 資產：{card['id']}")
        return candidate

    def list_cards(self, concept: str = "") -> list[dict[str, Any]]:
        recommended_id, recommendation = self.recommend(concept)
        results = []
        for card in self._cards:
            path = self._source_path(card)
            results.append(
                {
                    **card,
                    "available": path.is_file(),
                    "source_status": "available" if path.is_file() else "missing",
                    "source_label": "卡源正常" if path.is_file() else "卡源失聯",
                    "default": card["id"] == self.default_id,
                    "recommended": card["id"] == recommended_id,
                    "recommendation": recommendation if card["id"] == recommended_id else "",
                }
            )
        return results

    def recommend(self, concept: str) -> tuple[str, str]:
        text = concept.casefold()
        if any(word in text for word in ("汽車", "車市", "車款", "試駕", "新車")):
            return "writer.chen_car", "內容提到汽車／車市，建議陳宏銘車版筆法"
        if any(word in text for word in ("財經", "金融", "企業", "營收", "投資", "股市", "銀行")):
            return "writer.chen_finance", "內容偏財經事件，建議陳宏銘財經筆法"
        if any(word in text for word in ("訪談", "專訪", "受訪", "人物", "逐字稿")):
            return "writer.dong_chengyu", "內容偏人物／訪談，建議董成瑜人物筆法"
        return self.default_id, "預設建議人物訪談筆法"

    def snapshot(self, card_id: str) -> dict[str, Any]:
        card = next((item for item in self._cards if item["id"] == card_id), None)
        if not card:
            raise WriterFailure("writer_card_not_found", f"找不到寫手卡：{card_id}")
        path = self._source_path(card)
        if not path.is_file():
            raise WriterFailure("writer_card_source_missing", f"寫手卡源失聯：{card['name']}")
        try:
            content = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            raise WriterFailure("writer_card_unreadable", f"寫手卡無法讀取：{exc}") from exc
        return {
            **card,
            "resolved_path": str(path),
            "content": content,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }


def _string_list(value: Any, label: str, warnings: list[str]) -> list[str]:
    if value is None:
        warnings.append(f"{label} 缺少，已正規化為空清單")
        return []
    if isinstance(value, str):
        rows = [item.strip(" -\t") for item in value.splitlines() if item.strip(" -\t")]
        warnings.append(f"{label} 原為字串，已正規化為清單")
        return rows
    if not isinstance(value, list):
        warnings.append(f"{label} 類型不正確，已正規化為空清單")
        return []
    rows = []
    for item in value:
        if isinstance(item, str) and item.strip():
            rows.append(item.strip())
        elif isinstance(item, dict):
            text = "；".join(f"{key}：{val}" for key, val in item.items() if str(val).strip())
            if text:
                rows.append(text[:1200])
        elif item is not None:
            rows.append(str(item)[:1200])
    return rows[:100]


def normalize_writer_output(raw_text: str) -> tuple[str, dict[str, Any], list[str]]:
    """Keep usable prose even when report metadata is imperfect."""
    warnings: list[str] = []
    clean = raw_text.strip().lstrip("\ufeff")
    if clean.startswith("```") and clean.endswith("```"):
        clean = re.sub(r"^```(?:json|markdown|md|text)?\s*", "", clean, count=1, flags=re.I)
        clean = re.sub(r"\s*```$", "", clean, count=1)
        warnings.append("輸出外層 code fence 已移除")
    draft: Any = None
    report: Any = None
    marker = None
    try:
        parsed = json.loads(clean)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        draft = parsed.get("draft", parsed.get("content"))
        report = parsed.get("report", parsed)
    else:
        marker = re.search(r"<<<DRAFT>>>\s*(.*?)\s*<<<REPORT(?:_JSON)?>>>\s*(.*)$", clean, re.S | re.I)
        if marker:
            draft = marker.group(1).strip()
            report_text = marker.group(2).strip()
            report_text = re.sub(r"^```json\s*|\s*```$", "", report_text, flags=re.I)
            try:
                report = json.loads(report_text)
            except json.JSONDecodeError:
                warnings.append("寫手報告不是有效 JSON，已保留正文並正規化為空報告")
                report = {}
        else:
            draft = clean
            report = {}
            warnings.append("輸出未含標準標記，已將可用文字視為正文")
    draft = str(draft or "").strip()
    if not draft or not re.search(r"[\w\u3400-\u9fff]", draft):
        raise WriterFailure("writer_output_unusable", "寫手引擎沒有產生可用正文")
    if marker is None and not isinstance(parsed, dict):
        control_failure = re.search(
            r"(rate.?limit|quota|額度|配額|登入|login|authentication|api.?key|執行失敗|無法協助|cannot comply|error[:：])",
            draft,
            re.I,
        )
        if control_failure and len(draft) < 500:
            raise WriterFailure("writer_output_unusable", "寫手引擎只回傳錯誤／拒答訊息，沒有可用正文")
        if len(re.sub(r"\s", "", draft)) < 20:
            raise WriterFailure("writer_output_unusable", "寫手引擎輸出過短且沒有正文契約標記")
    if not isinstance(report, dict):
        warnings.append("寫手報告不是物件，已正規化為空報告")
        report = {}
    known = {"speaker_assessment", "length_note", "gaps", "names_to_verify"}
    unknown = sorted(set(report) - known - {"draft", "content", "report"})
    if unknown:
        warnings.append("寫手報告未知欄位已忽略：" + "、".join(unknown))
    length_note = report.get("length_note")
    if length_note is None:
        warnings.append("length_note 缺少，已正規化為空字串")
        length_note = ""
    elif not isinstance(length_note, str):
        length_note = str(length_note)
        warnings.append("length_note 已正規化為字串")
    normalized = {
        "speaker_assessment": _string_list(report.get("speaker_assessment"), "speaker_assessment", warnings),
        "length_note": length_note.strip()[:2000],
        "gaps": _string_list(report.get("gaps"), "gaps", warnings),
        "names_to_verify": _string_list(report.get("names_to_verify"), "names_to_verify", warnings),
    }
    return draft, normalized, warnings


class WriterEngineAdapter:
    def __init__(self, workdir: Path, timeout_seconds: int = 600):
        self.workdir = workdir.resolve()
        self.timeout_seconds = timeout_seconds

    def _prompt(self, title: str, content: str, card: dict[str, Any], target_length: int) -> str:
        return f"""你是新聞寫手。請依寫手卡整理逐字稿，禁止新增素材沒有的事實。

硬規則：
1. 分離講者並保留歸因；判不出時寫「未能辨識講者」，不得把多人揉成一人。
2. 目標約 {target_length} 字；素材不足寧可短，不灌水，報告說明還缺什麼。
3. 缺日期、地點、人數、場次、姓名、職稱、單位正名，列入缺口，正文不可腦補。
4. 疑似聽錯專名保留原樣，正文行內標「〔疑為Ｘ，待查證〕」，禁止直接換成推測值。
5. 使用繁體中文，只輸出下列契約，不要前言或 Markdown code fence。

<<<DRAFT>>>
可讀新聞初稿（只放正文，不放報告）
<<<REPORT_JSON>>>
{{"speaker_assessment":["講者與判讀依據"],"length_note":"字數與素材說明","gaps":["缺口"],"names_to_verify":["原樣專名；疑為X；推測依據"]}}

文件標題：{title}

寫手卡（整檔唯讀快照）：
{card['content']}

原始素材：
{content}
"""

    def _invoke(self, engine: str, prompt: str) -> tuple[str, dict[str, Any]]:
        factory = ENGINE_COMMANDS.get(engine)
        if not factory:
            raise WriterFailure("writer_engine_invalid", f"不支援的寫手引擎：{engine}")
        command = factory(prompt)
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=self.workdir,
                env=environment,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise WriterFailure("writer_engine_timeout", f"{engine} 逾時（{self.timeout_seconds} 秒）") from exc
        except OSError as exc:
            raise WriterFailure("writer_engine_start_failed", f"無法啟動 {engine}：{exc}") from exc
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[:1000]
            raise WriterFailure(
                "writer_engine_failed",
                f"{engine} 執行失敗（exit {completed.returncode}）：{detail or '沒有錯誤輸出'}",
                {"engine": engine, "elapsed_ms": elapsed_ms},
            )
        return completed.stdout, {
            "engine": engine,
            "elapsed_ms": elapsed_ms,
            "raw_output_hash": hashlib.sha256(completed.stdout.encode("utf-8")).hexdigest(),
        }

    def run_rewrite(
        self,
        title: str,
        content: str,
        card: dict[str, Any],
        target_length: int,
        progress: Callable[[str, bool], None] | None = None,
    ) -> dict[str, Any]:
        prompt = self._prompt(title, content, card, target_length)
        failures: list[dict[str, str]] = []
        requested = card["engine"]
        engines = (requested, card["fallback_engine"])
        for index, engine in enumerate(engines):
            proxy = index == 1
            if progress:
                progress(engine, proxy)
            try:
                raw, invocation = self._invoke(engine, prompt)
                draft, report, warnings = normalize_writer_output(raw)
                provenance = {
                    "requested_engine": requested,
                    "actual_engine": engine,
                    "fallback_engine": card["fallback_engine"],
                    "proxy": proxy,
                    "proxy_label": "本稿由備位引擎代打" if proxy else "",
                    "fallback_reason": failures[0]["message"] if proxy and failures else "",
                    "card": {"id": card["id"], "sha256": card["sha256"], "path": card["resolved_path"]},
                    "target_length": target_length,
                    "input_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "output_hash": hashlib.sha256(draft.encode("utf-8")).hexdigest(),
                    "normalization_warnings": {"count": len(warnings), "messages": warnings},
                    "attempts": [*failures, {**invocation, "status": "complete"}],
                }
                return {
                    "contract_version": WRITER_CONTRACT_VERSION,
                    "workflow": {"id": "newsroom_writer", "status": "complete"},
                    "draft": draft,
                    "report": report,
                    "summary": {"draft_characters": len(re.sub(r"\s", "", draft)), "total": sum(len(value) if isinstance(value, list) else bool(value) for value in report.values())},
                    "provenance": provenance,
                }
            except WriterFailure as exc:
                failures.append({"engine": engine, "code": exc.code, "message": exc.message, "status": "failed"})
        raise WriterFailure(
            "writer_all_engines_failed",
            "寫手主引擎與備位引擎皆失敗：" + "；".join(item["message"] for item in failures),
            {
                "requested_engine": requested,
                "fallback_engine": card["fallback_engine"],
                "card": {"id": card["id"], "sha256": card["sha256"], "path": card["resolved_path"]},
                "target_length": target_length,
                "input_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "attempts": failures,
            },
        )


class WriterJobManager:
    def __init__(self, repository: DocumentRepository, registry: WriterCardRegistry, adapter: WriterEngineAdapter):
        self.repository = repository
        self.registry = registry
        self.adapter = adapter
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def start(self, document_id: str, revision_id: str, title: str, content: str, card_id: str, target_length: int) -> dict[str, Any]:
        if not isinstance(target_length, int) or isinstance(target_length, bool) or not 200 <= target_length <= 20000:
            raise ValueError("目標字數必須是 200 到 20000 的整數")
        card = self.registry.snapshot(card_id)
        with self._lock:
            for job in self._jobs.values():
                if (
                    job["document_id"] == document_id
                    and job["revision_id"] == revision_id
                    and job["writer_card_id"] == card_id
                    and job["target_length"] == target_length
                    and job["status"] in {"queued", "running"}
                ):
                    return dict(job)
            job_id = str(uuid.uuid4())
            job = {
                "id": job_id,
                "document_id": document_id,
                "revision_id": revision_id,
                "writer_card_id": card_id,
                "target_length": target_length,
                "status": "queued",
                "pass": "queued",
                "proxy": False,
                "created_at": utc_now(),
                "started_at": None,
                "completed_at": None,
                "run_id": None,
                "generated_revision_id": None,
                "error": None,
            }
            self._jobs[job_id] = job
        threading.Thread(
            target=self._run,
            args=(job_id, document_id, revision_id, title, content, card, target_length),
            name=f"writer-{job_id[:8]}",
            daemon=True,
        ).start()
        return dict(job)

    def _update(self, job_id: str, **values: Any) -> None:
        with self._lock:
            self._jobs[job_id].update(values)

    def _run(self, job_id: str, document_id: str, revision_id: str, title: str, content: str, card: dict[str, Any], target_length: int) -> None:
        self._update(job_id, status="running", started_at=utc_now(), **{"pass": card["engine"]})
        try:
            output = self.adapter.run_rewrite(
                title,
                content,
                card,
                target_length,
                progress=lambda engine, proxy: self._update(job_id, **{"pass": engine}, proxy=proxy),
            )
            updated = self.repository.save_revision(
                document_id,
                title,
                output["draft"],
                actor=f"writer:{card['id']}",
                note=f"寫手出稿・{card['name']}・{output['provenance']['actual_engine']}",
                force=True,
                expected_current_revision_id=revision_id,
            )
            generated_id = updated["current_revision_id"]
            self.repository.save_source_hint(document_id, generated_id, self.repository.get_source_hint(document_id, revision_id))
            self.repository.copy_source_decisions(document_id, revision_id, generated_id)
            output["provenance"].update(
                {
                    "source_revision_id": revision_id,
                    "generated_revision_id": generated_id,
                    "source_content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                }
            )
            output.pop("draft", None)
            run = self.repository.record_run(
                document_id,
                generated_id,
                "writer_rewrite",
                output,
                card_id=card["id"],
                status="complete",
                engine=str(output["provenance"]["actual_engine"]),
            )
            self._update(
                job_id,
                status="complete",
                completed_at=utc_now(),
                run_id=run["id"],
                generated_revision_id=generated_id,
                proxy=bool(output["provenance"]["proxy"]),
                **{"pass": "complete"},
            )
        except WriterFailure as exc:
            self._record_failure(job_id, document_id, revision_id, card, exc)
        except ValueError as exc:
            failure = WriterFailure("writer_revision_stale", str(exc), {"input_hash": hashlib.sha256(content.encode("utf-8")).hexdigest()})
            self._record_failure(job_id, document_id, revision_id, card, failure)
        except Exception as exc:
            self._record_failure(job_id, document_id, revision_id, card, WriterFailure("writer_internal_error", f"寫手背景工作失敗：{exc}"))

    def _record_failure(self, job_id: str, document_id: str, revision_id: str, card: dict[str, Any], exc: WriterFailure) -> None:
        output = {
            "contract_version": WRITER_CONTRACT_VERSION,
            "workflow": {"id": "newsroom_writer", "status": "failed"},
            "error": {"code": exc.code, "message": exc.message},
            "provenance": {
                "card": {"id": card["id"], "sha256": card["sha256"], "path": card["resolved_path"]},
                **exc.provenance,
                "source_revision_id": revision_id,
            },
            "summary": {"total": 0},
        }
        engine = str(output["provenance"].get("actual_engine") or output["provenance"].get("requested_engine") or card["engine"])
        run = self.repository.record_run(document_id, revision_id, "writer_rewrite", output, card_id=card["id"], status="failed", engine=engine)
        self._update(job_id, status="failed", completed_at=utc_now(), run_id=run["id"], error=output["error"], **{"pass": "failed"})

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError("找不到寫手工作")
            result = dict(job)
        if result["run_id"]:
            result["run"] = self.repository.get_run(result["run_id"])
        return result

    def latest(self, document_id: str, revision_id: str) -> dict[str, Any] | None:
        with self._lock:
            candidates = [dict(job) for job in self._jobs.values() if job["document_id"] == document_id and job["revision_id"] == revision_id]
        return candidates[-1] if candidates else None
