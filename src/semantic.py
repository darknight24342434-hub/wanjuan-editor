from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .catalog import BaiguiCatalog
from .repository import DocumentRepository


CONTRACT_VERSION = "newsroom_semantic_v2"
EDITOR_FUNCTIONS = ("正確性", "可讀性", "文筆", "下標", "結構")
CHIEF_FUNCTIONS = ("指錯", "糾漏", "建議")
JOB_STATUSES = {"queued", "running", "complete", "failed"}
SAFE_SEMANTIC_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_hash(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class SemanticContractError(ValueError):
    """The engine returned JSON that does not satisfy the newsroom contract."""


class SemanticFailure(RuntimeError):
    def __init__(self, code: str, message: str, provenance: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.provenance = provenance or {}


@dataclass(frozen=True)
class EngineConfig:
    name: str
    command: tuple[str, ...]
    timeout_seconds: int
    output: str
    network_disclosure: str
    fallback: EngineConfig | None = None


def load_engine_config(path: Path) -> EngineConfig:
    if not path.is_file():
        raise SemanticFailure("engine_config_missing", f"找不到語義引擎設定：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemanticFailure("engine_config_invalid", f"語義引擎設定無法解析：{exc}") from exc
    if not isinstance(raw, dict):
        raise SemanticFailure("engine_config_invalid", "語義引擎設定根節點必須是物件")
    return _parse_engine_config(raw)


def _parse_engine_config(raw: dict[str, Any], label: str = "語義引擎設定") -> EngineConfig:
    name = str(raw.get("name") or "").strip()
    command = raw.get("command")
    timeout = raw.get("timeout_seconds")
    output = str(raw.get("output") or "file").strip()
    disclosure = str(raw.get("network_disclosure") or "").strip()
    if not name:
        raise SemanticFailure("engine_config_invalid", f"{label}缺少 name")
    if not isinstance(command, list) or not command or not all(
        isinstance(item, str) and item for item in command
    ):
        raise SemanticFailure("engine_config_invalid", f"{label} command 必須是非空字串陣列")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 600:
        raise SemanticFailure("engine_config_invalid", f"{label} timeout_seconds 必須是 1 到 600 的整數")
    if output not in {"file", "stdout"}:
        raise SemanticFailure("engine_config_invalid", f"{label} output 只支援 file 或 stdout")
    if not disclosure:
        raise SemanticFailure("engine_config_invalid", f"{label}必須明示 network_disclosure")
    fallback_raw = raw.get("fallback")
    if fallback_raw is not None and not isinstance(fallback_raw, dict):
        raise SemanticFailure("engine_config_invalid", "fallback 必須是引擎設定物件")
    fallback = _parse_engine_config(fallback_raw, "編輯備位引擎設定") if isinstance(fallback_raw, dict) else None
    if fallback and fallback.fallback:
        raise SemanticFailure("engine_config_invalid", "編輯備位引擎不可再設定下一層備位")
    return EngineConfig(name, tuple(command), timeout, output, disclosure, fallback)


def _annotation_schema(functions: tuple[str, ...], chief: bool = False) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "function": {"type": "string", "enum": list(functions)},
        "severity": {"type": "string", "enum": ["warning", "note"]},
        "message": {"type": "string", "minLength": 1, "maxLength": 2000},
        "excerpt": {"type": "string", "maxLength": 500},
        "start": {"type": ["integer", "null"]},
        "end": {"type": ["integer", "null"]},
        "structure_block_id": {"type": ["string", "null"]},
    }
    if chief:
        properties["questioned_annotation_id"] = {"type": ["string", "null"]}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def editor_output_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["annotations", "headline_candidates"],
        "properties": {
            "annotations": {
                "type": "array",
                "minItems": 5,
                "maxItems": 60,
                "items": _annotation_schema(EDITOR_FUNCTIONS),
            },
            "headline_candidates": {
                "type": "array",
                "minItems": 2,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["main_title", "subtitle", "angle"],
                    "properties": {
                        "main_title": {"type": "string", "minLength": 1, "maxLength": 180},
                        "subtitle": {"type": "string", "maxLength": 180},
                        "angle": {"type": "string", "minLength": 1, "maxLength": 300},
                    },
                },
            },
        },
    }


def chief_output_schema() -> dict[str, Any]:
    recommendation = {
        "type": "object",
        "additionalProperties": False,
        "required": ["text", "reason"],
        "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": 1200},
            "reason": {"type": "string", "minLength": 1, "maxLength": 600},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "annotations",
            "headline_recommendation",
            "lead_recommendation",
            "angle_recommendation",
        ],
        "properties": {
            "annotations": {
                "type": "array",
                "minItems": 3,
                "maxItems": 60,
                "items": _annotation_schema(CHIEF_FUNCTIONS, chief=True),
            },
            "headline_recommendation": recommendation,
            "lead_recommendation": recommendation,
            "angle_recommendation": recommendation,
        },
    }


def _warn(normalized_warnings: list[str] | None, message: str) -> None:
    if normalized_warnings is not None:
        normalized_warnings.append(message)


def _require_keys(value: dict[str, Any], required: set[str], label: str) -> None:
    """僅供「整包無法解讀」時使用；欄位層級問題一律改記警示放行。"""
    missing = required - set(value)
    if missing:
        raise SemanticContractError(f"{label} 缺少必要內容欄位：{'、'.join(sorted(missing))}")


def _discard_unknown_keys(
    value: dict[str, Any],
    allowed: set[str],
    label: str,
    normalized_warnings: list[str] | None,
) -> dict[str, Any]:
    unknown = sorted(set(value) - allowed)
    if unknown:
        _warn(
            normalized_warnings,
            f"{label} 已移除未知欄位：{'、'.join(unknown)}",
        )
    return {key: value[key] for key in allowed if key in value}


def _normalize_internal_id(
    value: Any,
    prefix: str,
    seen: set[str],
    label: str,
    normalized_warnings: list[str] | None,
) -> str:
    if (
        isinstance(value, str)
        and len(value) <= 64
        and SAFE_SEMANTIC_ID.fullmatch(value)
        and value.startswith(prefix)
        and value not in seen
    ):
        normalized = value
    else:
        suffix = 1
        normalized = f"{prefix}{suffix}"
        while normalized in seen:
            suffix += 1
            normalized = f"{prefix}{suffix}"
        reason = "缺少" if value is None else "格式無效或重複"
        _warn(
            normalized_warnings,
            f"{label} {reason}，已由伺服器重新配置為 {normalized}",
        )
    seen.add(normalized)
    return normalized


def _bounded_text(
    value: Any,
    label: str,
    maximum: int,
    allow_empty: bool = False,
    *,
    normalized_warnings: list[str] | None = None,
) -> str:
    """把值收斂成有長度上限的字串。只記警示，不退件。"""
    if isinstance(value, str):
        text = value
    elif value is None:
        text = ""
        _warn(normalized_warnings, f"{label} 缺少，已補為空字串")
    else:
        text = str(value)
        _warn(normalized_warnings, f"{label} 不是字串，已轉為文字")
    if not allow_empty and not text.strip():
        _warn(normalized_warnings, f"{label} 為空白")
    if len(text) > maximum:
        _warn(normalized_warnings, f"{label} 超過 {maximum} 字，已截斷")
        text = text[:maximum]
    return text


def _validate_annotations(
    rows: Any,
    functions: tuple[str, ...],
    content: str,
    structure: dict[str, Any],
    editor_ids: set[str] | None = None,
    *,
    normalized_warnings: list[str] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        _warn(normalized_warnings, "annotations 不是清單，本回合視為無批註")
        rows = []
    elif not rows:
        _warn(normalized_warnings, "annotations 為空清單")
    if len(rows) > 60:
        _warn(normalized_warnings, f"annotations 超過 60 項，已保留前 60 項（原 {len(rows)} 項）")
        rows = rows[:60]
    blocks = {str(item.get("id")): item for item in structure.get("blocks", [])}
    expected_keys = {
        "id", "function", "severity", "message", "excerpt", "start", "end", "structure_block_id"
    }
    chief = editor_ids is not None
    if chief:
        expected_keys.add("questioned_annotation_id")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    found_functions: set[str] = set()
    for index, raw in enumerate(rows):
        label = f"annotations[{index}]"
        if not isinstance(raw, dict):
            _warn(normalized_warnings, f"{label} 不是物件，已略過這一條")
            continue
        if not str(raw.get("message") or "").strip():
            _warn(normalized_warnings, f"{label} 沒有批註內容，已略過這一條")
            continue
        item = _discard_unknown_keys(raw, expected_keys, label, normalized_warnings)
        anchor_fields = {"excerpt", "start", "end", "structure_block_id"}
        if chief:
            anchor_fields.add("questioned_annotation_id")
        missing_anchor_fields = sorted(anchor_fields - set(item))
        if missing_anchor_fields:
            _warn(
                normalized_warnings,
                f"{label} 缺少錨定欄位，已補為空值：{'、'.join(missing_anchor_fields)}",
            )
        required_prefix = "chief-" if chief else "editor-"
        annotation_id = _normalize_internal_id(
            item.get("id"), required_prefix, seen, f"{label}.id", normalized_warnings
        )
        function = item.get("function")
        if function not in functions:
            fallback = functions[-1]
            _warn(
                normalized_warnings,
                f"{label}.function「{function}」不在清單內，已歸到「{fallback}」",
            )
            function = fallback
        found_functions.add(function)
        severity = item.get("severity")
        if severity not in {"warning", "note"}:
            _warn(normalized_warnings, f"{label}.severity 無效或缺少，已正規化為 note")
            severity = "note"
        message = _bounded_text(
            item.get("message"),
            f"{label}.message",
            2000,
            normalized_warnings=normalized_warnings,
        )

        excerpt = item.get("excerpt", "")
        if not isinstance(excerpt, str):
            _warn(normalized_warnings, f"{label}.excerpt 不是字串，已正規化為空字串")
            excerpt = ""
        elif len(excerpt) > 500:
            _warn(normalized_warnings, f"{label}.excerpt 超過 500 字，已清空並取消字元座標")
            excerpt = ""

        block_id = item.get("structure_block_id")
        if block_id is not None and (not isinstance(block_id, str) or block_id not in blocks):
            _warn(normalized_warnings, f"{label}.structure_block_id 不存在，已正規化為 null")
            block_id = None

        raw_start, raw_end = item.get("start"), item.get("end")
        normalized_start: int | None = None
        normalized_end: int | None = None
        if excerpt:
            occurrence = -1
            found_in_block = False
            if block_id:
                block = blocks[block_id]
                occurrence = content.find(
                    excerpt,
                    int(block["start"]),
                    int(block["end"]),
                )
                found_in_block = occurrence >= 0
            if occurrence < 0:
                occurrence = content.find(excerpt)
            if occurrence >= 0:
                normalized_start = occurrence
                normalized_end = occurrence + len(excerpt)
                coordinates_are_exact_integers = (
                    type(raw_start) is int and type(raw_end) is int
                )
                if not coordinates_are_exact_integers or (
                    raw_start,
                    raw_end,
                ) != (normalized_start, normalized_end):
                    _warn(
                        normalized_warnings,
                        f"{label}.start/end 已依 excerpt 重錨為 "
                        f"{normalized_start}:{normalized_end}",
                    )
                if block_id and not found_in_block:
                    _warn(
                        normalized_warnings,
                        f"{label}.excerpt 不在指定結構區塊內，已改用稿件全文第一處",
                    )
            else:
                if raw_start is not None or raw_end is not None:
                    detail = "，原座標已清除"
                else:
                    detail = ""
                _warn(
                    normalized_warnings,
                    f"{label}.excerpt 在稿件中找不到，start/end 已正規化為 null{detail}",
                )
        elif raw_start is not None or raw_end is not None:
            _warn(
                normalized_warnings,
                f"{label}.excerpt 為空，start/end 已正規化為 null",
            )

        item["id"] = annotation_id
        item["function"] = function
        item["severity"] = severity
        item["message"] = message
        item["excerpt"] = excerpt
        item["start"] = normalized_start
        item["end"] = normalized_end
        item["structure_block_id"] = block_id
        if chief:
            target = item.get("questioned_annotation_id")
            if not isinstance(target, str) or target not in (editor_ids or set()):
                if target is not None or function == "指錯":
                    _warn(
                        normalized_warnings,
                        f"{label}.questioned_annotation_id 無有效編輯批註可連結，已正規化為 null",
                    )
                target = None
            item["questioned_annotation_id"] = target
        normalized.append(item)
    missing = set(functions) - found_functions
    if missing:
        _warn(normalized_warnings, "本回合沒有這些功能的批註：" + "、".join(sorted(missing)))
    return normalized


def validate_editor_output(
    raw: Any,
    content: str,
    structure: dict[str, Any],
    *,
    normalized_warnings: list[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SemanticContractError("編輯審輸出根節點必須是物件")
    keys = {"annotations", "headline_candidates"}
    missing_keys = sorted(keys - set(raw))
    if missing_keys:
        _warn(normalized_warnings, "編輯審輸出缺少欄位，已補為空值：" + "、".join(missing_keys))
    pass_output = _discard_unknown_keys(raw, keys, "編輯審輸出", normalized_warnings)
    annotations = _validate_annotations(
        pass_output.get("annotations"),
        EDITOR_FUNCTIONS,
        content,
        structure,
        normalized_warnings=normalized_warnings,
    )
    candidates = pass_output.get("headline_candidates")
    if not isinstance(candidates, list):
        _warn(normalized_warnings, "下標候選不是清單，本回合視為沒有候選")
        candidates = []
    elif len(candidates) < 2:
        _warn(normalized_warnings, f"下標候選只有 {len(candidates)} 組，少於建議的 2 組")
    if len(candidates) > 3:
        _warn(normalized_warnings, f"下標候選超過 3 組，已保留前 3 組（原 {len(candidates)} 組）")
        candidates = candidates[:3]
    seen: set[str] = set()
    normalized_candidates = []
    for index, candidate in enumerate(candidates):
        label = f"下標候選[{index}]"
        if not isinstance(candidate, dict):
            _warn(normalized_warnings, f"{label} 不是物件，已略過這一組")
            continue
        if not str(candidate.get("main_title") or "").strip():
            _warn(normalized_warnings, f"{label} 沒有主標，已略過這一組")
            continue
        candidate = _discard_unknown_keys(
            candidate,
            {"id", "main_title", "subtitle", "angle"},
            label,
            normalized_warnings,
        )
        if "subtitle" not in candidate:
            candidate["subtitle"] = ""
            _warn(normalized_warnings, f"{label}.subtitle 缺少，已正規化為空字串")
        candidate_id = _normalize_internal_id(
            candidate.get("id"), "headline-", seen, f"{label}.id", normalized_warnings
        )
        normalized_candidate = dict(candidate)
        normalized_candidate["main_title"] = _bounded_text(
            candidate.get("main_title"), f"{label}.main_title", 180,
            normalized_warnings=normalized_warnings,
        )
        normalized_candidate["subtitle"] = _bounded_text(
            candidate.get("subtitle"), f"{label}.subtitle", 180, allow_empty=True,
            normalized_warnings=normalized_warnings,
        )
        normalized_candidate["angle"] = _bounded_text(
            candidate.get("angle"), f"{label}.angle", 300,
            normalized_warnings=normalized_warnings,
        )
        normalized_candidate["id"] = candidate_id
        normalized_candidates.append(normalized_candidate)
    return {"annotations": annotations, "headline_candidates": normalized_candidates}


def validate_chief_output(
    raw: Any,
    content: str,
    structure: dict[str, Any],
    editor_output: dict[str, Any],
    *,
    normalized_warnings: list[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SemanticContractError("總編審輸出根節點必須是物件")
    keys = {
        "annotations", "headline_recommendation", "lead_recommendation", "angle_recommendation"
    }
    missing_keys = sorted(keys - set(raw))
    if missing_keys:
        _warn(normalized_warnings, "總編審輸出缺少欄位，已補為空值：" + "、".join(missing_keys))
    pass_output = _discard_unknown_keys(raw, keys, "總編審輸出", normalized_warnings)
    editor_ids = {item["id"] for item in editor_output["annotations"]}
    annotations = _validate_annotations(
        pass_output.get("annotations"),
        CHIEF_FUNCTIONS,
        content,
        structure,
        editor_ids=editor_ids,
        normalized_warnings=normalized_warnings,
    )
    result: dict[str, Any] = {"annotations": annotations}
    for key in ("headline_recommendation", "lead_recommendation", "angle_recommendation"):
        item = pass_output.get(key)
        if not isinstance(item, dict):
            _warn(normalized_warnings, f"{key} 不是物件，已補為空建議")
            item = {}
        item = _discard_unknown_keys(
            item, {"text", "reason"}, key, normalized_warnings
        )
        result[key] = {
            "text": _bounded_text(
                item.get("text"), f"{key}.text", 1200, allow_empty=True,
                normalized_warnings=normalized_warnings,
            ),
            "reason": _bounded_text(
                item.get("reason"), f"{key}.reason", 600, allow_empty=True,
                normalized_warnings=normalized_warnings,
            ),
        }
    return result


class SemanticEngineAdapter:
    """Run a two-pass newsroom review through a local command template."""

    def __init__(
        self,
        catalog: BaiguiCatalog,
        config_path: Path,
        workdir: Path,
        temp_root: Path,
    ):
        self.catalog = catalog
        self.config_path = config_path.resolve()
        self.workdir = workdir.resolve()
        self.temp_root = temp_root.resolve()

    def _invoke(
        self,
        config: EngineConfig,
        role: str,
        prompt: dict[str, Any],
        schema: dict[str, Any],
    ) -> tuple[Any, dict[str, Any]]:
        self.temp_root.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        schema_path = self.temp_root / f"schema-{token}.json"
        output_path = self.temp_root / f"output-{token}.json"
        schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
        values = {
            "schema_path": str(schema_path),
            "output_path": str(output_path),
            "role": role,
        }
        try:
            try:
                command = [item.format_map(values) for item in config.command]
            except KeyError as exc:
                raise SemanticFailure("engine_config_invalid", f"command 使用未知占位符：{exc}") from exc
            started = time.perf_counter()
            environment = os.environ.copy()
            environment["PYTHONUTF8"] = "1"
            try:
                completed = subprocess.run(
                    command,
                    input=json.dumps(prompt, ensure_ascii=False),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=self.workdir,
                    env=environment,
                    timeout=config.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise SemanticFailure(
                    "engine_timeout",
                    f"{role}逾時（{config.timeout_seconds} 秒）",
                    {"command": command, "role": role},
                ) from exc
            except OSError as exc:
                raise SemanticFailure(
                    "engine_start_failed",
                    f"無法啟動語義引擎：{exc}",
                    {"command": command, "role": role},
                ) from exc
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()[:1000]
                raise SemanticFailure(
                    "engine_failed",
                    f"{role}執行失敗（exit {completed.returncode}）：{detail or '沒有錯誤輸出'}",
                    {"command": command, "role": role, "elapsed_ms": elapsed_ms},
                )
            if config.output == "file":
                if not output_path.is_file():
                    raise SemanticFailure(
                        "engine_output_missing",
                        f"{role}沒有產生設定要求的輸出檔",
                        {"command": command, "role": role, "elapsed_ms": elapsed_ms},
                    )
                raw_text = output_path.read_text(encoding="utf-8-sig")
            else:
                raw_text = completed.stdout
            output_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
            try:
                parsed = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                raise SemanticFailure(
                    "engine_output_invalid_json",
                    f"{role}輸出不是有效 JSON：{exc}",
                    {
                        "command": command,
                        "role": role,
                        "elapsed_ms": elapsed_ms,
                        "raw_output_hash": output_hash,
                    },
                ) from exc
            return parsed, {
                "role": role,
                "command": command,
                "elapsed_ms": elapsed_ms,
                "raw_output_hash": output_hash,
            }
        finally:
            schema_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)

    def run_review(
        self,
        title: str,
        content: str,
        structure: dict[str, Any],
        progress: Callable[[str], None] | None = None,
        writer_actual_engine: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        provenance: dict[str, Any] = {
            "engine": "unconfigured",
            "engine_config_path": str(self.config_path),
            "structure_hash": json_hash(structure),
            "card_snapshots": {},
            "input_hashes": {},
            "output_hashes": {},
            "passes": [],
        }
        try:
            editor_card = self.catalog.get_asset("editors", "editor.news")
            chief_card = self.catalog.get_asset("editors", "editor.baigui_editor_in_chief")
            provenance["card_snapshots"] = {
                "editor": {"id": editor_card["id"], "sha256": editor_card["sha256"]},
                "chief": {"id": chief_card["id"], "sha256": chief_card["sha256"]},
            }
            editor_prompt = {
                "contract_version": CONTRACT_VERSION,
                "role": "editor",
                "role_card": {"id": editor_card["id"], "content": editor_card["content"]},
                "instructions": [
                    "只輸出符合 schema 的 JSON，不要 Markdown。",
                    "不得新增稿件沒有的事實；正確性問題只能標需查證並說明理由。",
                    "五項功能都至少一條；下標另給 2 到 3 組主標、副標與取向。",
                    "優先使用 structure_block_id 錨定；不確定精確字元座標時 start/end 填 null。",
                ],
                "article": {"title": title, "content": content},
                "structure": structure,
                "output_schema": editor_output_schema(),
            }
            provenance["input_hashes"]["editor"] = json_hash(editor_prompt)
            requested_config = load_engine_config(self.config_path)
            config = requested_config
            engine_proxy = False
            engine_proxy_reason = ""
            if writer_actual_engine and requested_config.name == writer_actual_engine:
                if not requested_config.fallback or requested_config.fallback.name == writer_actual_engine:
                    raise SemanticFailure(
                        "editor_model_conflict",
                        f"寫手與編輯不可連續使用同一模型：{writer_actual_engine}；沒有可用的異模型編輯備位",
                        provenance,
                    )
                config = requested_config.fallback
                engine_proxy = True
                engine_proxy_reason = f"寫手實際引擎為 {writer_actual_engine}，編輯自動改用異模型備位"
            provenance["engine"] = config.name
            provenance["network_disclosure"] = config.network_disclosure
            provenance["engine_routing"] = {
                "writer_actual_engine": writer_actual_engine,
                "requested_editor_engine": requested_config.name,
                "actual_editor_engine": config.name,
                "proxy": engine_proxy,
                "proxy_label": "編輯由備位引擎代打" if engine_proxy else "",
                "reason": engine_proxy_reason,
            }
            if progress:
                progress("editor")
            raw_editor, editor_pass = self._invoke(
                config, "編輯審", editor_prompt, editor_output_schema()
            )
            provenance["passes"].append(editor_pass)
            editor_warnings: list[str] = []
            try:
                editor_output = validate_editor_output(
                    raw_editor,
                    content,
                    structure,
                    normalized_warnings=editor_warnings,
                )
            except SemanticContractError as exc:
                editor_pass["normalized_warnings"] = {
                    "count": len(editor_warnings),
                    "messages": editor_warnings,
                }
                raise SemanticFailure(
                    "engine_output_contract_failed",
                    f"編輯審輸出不合契約：{exc}",
                    {**provenance, "raw_output_hash": editor_pass["raw_output_hash"]},
                ) from exc
            editor_pass["normalized_warnings"] = {
                "count": len(editor_warnings),
                "messages": editor_warnings,
            }
            provenance["output_hashes"]["editor"] = json_hash(editor_output)

            chief_prompt = {
                "contract_version": CONTRACT_VERSION,
                "role": "editor_in_chief",
                "role_card": {"id": chief_card["id"], "content": chief_card["content"]},
                "instructions": [
                    "只輸出符合 schema 的 JSON，不要 Markdown。",
                    "逐條檢查編輯審；指錯必須用 questioned_annotation_id 連到被質疑的編輯批註。",
                    "糾漏只指出未查核、未採訪或未交代的缺口，不得替稿件補造事實。",
                    "三項功能都至少一條，並具體寫出更好的標、導言與切角。",
                    "優先使用 structure_block_id 錨定；不確定精確字元座標時 start/end 填 null。",
                ],
                "article": {"title": title, "content": content},
                "structure": structure,
                "editor_output": editor_output,
                "output_schema": chief_output_schema(),
            }
            provenance["input_hashes"]["chief"] = json_hash(chief_prompt)
            if progress:
                progress("chief")
            raw_chief, chief_pass = self._invoke(
                config, "總編審", chief_prompt, chief_output_schema()
            )
            provenance["passes"].append(chief_pass)
            chief_warnings: list[str] = []
            try:
                chief_output = validate_chief_output(
                    raw_chief,
                    content,
                    structure,
                    editor_output,
                    normalized_warnings=chief_warnings,
                )
            except SemanticContractError as exc:
                chief_pass["normalized_warnings"] = {
                    "count": len(chief_warnings),
                    "messages": chief_warnings,
                }
                raise SemanticFailure(
                    "engine_output_contract_failed",
                    f"總編審輸出不合契約：{exc}",
                    {**provenance, "raw_output_hash": chief_pass["raw_output_hash"]},
                ) from exc
            chief_pass["normalized_warnings"] = {
                "count": len(chief_warnings),
                "messages": chief_warnings,
            }
            provenance["output_hashes"]["chief"] = json_hash(chief_output)
            provenance["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
            return {
                "contract_version": CONTRACT_VERSION,
                "workflow": {"id": "chinatimes_semantic_v2", "status": "complete"},
                "structure": structure,
                "editor": editor_output,
                "chief": chief_output,
                "summary": {
                    "editor_annotations": len(editor_output["annotations"]),
                    "chief_annotations": len(chief_output["annotations"]),
                    "headline_candidates": len(editor_output["headline_candidates"]),
                    "total": len(editor_output["annotations"]) + len(chief_output["annotations"]),
                },
                "provenance": provenance,
            }
        except SemanticFailure as exc:
            merged = {**provenance, **exc.provenance}
            merged["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
            raise SemanticFailure(exc.code, exc.message, merged) from exc
        except Exception as exc:
            provenance["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
            raise SemanticFailure("semantic_internal_error", f"語義審內部錯誤：{exc}", provenance) from exc


class SemanticJobManager:
    """In-memory background state with durable completed/failed runs in the repository."""

    def __init__(
        self,
        repository: DocumentRepository,
        adapter: SemanticEngineAdapter,
    ):
        self.repository = repository
        self.adapter = adapter
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def start(
        self,
        document_id: str,
        revision_id: str,
        title: str,
        content: str,
        structure: dict[str, Any],
        writer_actual_engine: str | None = None,
    ) -> dict[str, Any]:
        structure_digest = json_hash(structure)
        with self._lock:
            for job in self._jobs.values():
                if (
                    job["document_id"] == document_id
                    and job["revision_id"] == revision_id
                    and job.get("structure_hash") == structure_digest
                    and job.get("writer_actual_engine") == writer_actual_engine
                    and job["status"] in {"queued", "running"}
                ):
                    return dict(job)
            job_id = str(uuid.uuid4())
            job = {
                "id": job_id,
                "document_id": document_id,
                "revision_id": revision_id,
                "structure_hash": structure_digest,
                "writer_actual_engine": writer_actual_engine,
                "status": "queued",
                "pass": "queued",
                "created_at": utc_now(),
                "started_at": None,
                "completed_at": None,
                "run_id": None,
                "error": None,
            }
            self._jobs[job_id] = job
        thread = threading.Thread(
            target=self._run,
            args=(job_id, document_id, revision_id, title, content, structure, writer_actual_engine),
            name=f"semantic-{job_id[:8]}",
            daemon=True,
        )
        thread.start()
        return dict(job)

    def _update(self, job_id: str, **values: Any) -> None:
        with self._lock:
            self._jobs[job_id].update(values)

    def _run(
        self,
        job_id: str,
        document_id: str,
        revision_id: str,
        title: str,
        content: str,
        structure: dict[str, Any],
        writer_actual_engine: str | None,
    ) -> None:
        self._update(job_id, status="running", started_at=utc_now(), **{"pass": "editor"})
        try:
            output = self.adapter.run_review(
                title,
                content,
                structure,
                progress=lambda role: self._update(job_id, **{"pass": role}),
                writer_actual_engine=writer_actual_engine,
            )
            run = self.repository.record_run(
                document_id,
                revision_id,
                "semantic_review",
                output,
                persona_id="editor.news+editor.baigui_editor_in_chief",
                status="complete",
                engine=str(output["provenance"]["engine"]),
            )
            self._update(
                job_id,
                status="complete",
                completed_at=utc_now(),
                run_id=run["id"],
                **{"pass": "complete"},
            )
        except SemanticFailure as exc:
            output = {
                "contract_version": CONTRACT_VERSION,
                "workflow": {"id": "chinatimes_semantic_v2", "status": "failed"},
                "error": {"code": exc.code, "message": exc.message},
                "provenance": exc.provenance,
                "summary": {"total": 0},
            }
            run = self.repository.record_run(
                document_id,
                revision_id,
                "semantic_review",
                output,
                persona_id="editor.news+editor.baigui_editor_in_chief",
                status="failed",
                engine=str(exc.provenance.get("engine") or "unconfigured"),
            )
            self._update(
                job_id,
                status="failed",
                completed_at=utc_now(),
                run_id=run["id"],
                error=output["error"],
                **{"pass": "failed"},
            )
        except Exception as exc:
            failure = {"code": "semantic_internal_error", "message": f"語義背景工作失敗：{exc}"}
            output = {
                "contract_version": CONTRACT_VERSION,
                "workflow": {"id": "chinatimes_semantic_v2", "status": "failed"},
                "error": failure,
                "provenance": {"engine": "unknown"},
                "summary": {"total": 0},
            }
            run = self.repository.record_run(
                document_id,
                revision_id,
                "semantic_review",
                output,
                persona_id="editor.news+editor.baigui_editor_in_chief",
                status="failed",
                engine="unknown",
            )
            self._update(
                job_id,
                status="failed",
                completed_at=utc_now(),
                run_id=run["id"],
                error=failure,
                **{"pass": "failed"},
            )

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError("找不到語義審工作")
            result = dict(job)
        if result["run_id"]:
            result["run"] = self.repository.get_run(result["run_id"])
        return result

    def latest(self, document_id: str, revision_id: str) -> dict[str, Any] | None:
        with self._lock:
            candidates = [
                dict(job)
                for job in self._jobs.values()
                if job["document_id"] == document_id and job["revision_id"] == revision_id
            ]
        return candidates[-1] if candidates else None
