from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from .semantic import (
    EngineConfig,
    SemanticEngineAdapter,
    SemanticFailure,
    json_hash,
    load_engine_config,
    utc_now,
)


MEDIA_CONTRACT_VERSION = "wanjuan_media_v1"
MAX_MEDIA_BYTES = 3 * 1024 * 1024 * 1024
ALLOWED_MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4a", ".mp3", ".wav"}
TARGET_CLIP_SECONDS = (10, 30, 60, 90)
SAFE_MEDIA_ID = re.compile(r"^[0-9a-f]{32}$")
SAFE_CLIP_NAME = re.compile(r"^clip_(10|30|60|90)s_[1-9][0-9]*\.mp4$")


class MediaFailure(RuntimeError):
    def __init__(self, code: str, message: str, provenance: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.provenance = provenance or {}


@dataclass(frozen=True)
class MediaConfig:
    whisper_model: str = "medium"
    device: str = "auto"
    language: str | None = "zh"
    max_upload_bytes: int = MAX_MEDIA_BYTES


class Transcriber(Protocol):
    def transcribe(self, source_path: Path, *, language: str | None = None) -> Any: ...


def load_media_config(path: Path) -> MediaConfig:
    if not path.is_file():
        raise MediaFailure("media_config_missing", f"找不到影音設定：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MediaFailure("media_config_invalid", f"影音設定無法解析：{exc}") from exc
    if not isinstance(raw, dict):
        raise MediaFailure("media_config_invalid", "影音設定根節點必須是物件")
    model = str(raw.get("whisper_model") or "").strip()
    device = str(raw.get("device") or "").strip().lower()
    language_raw = raw.get("language", "zh")
    maximum = raw.get("max_upload_bytes")
    if not model:
        raise MediaFailure("media_config_invalid", "whisper_model 不可空白")
    if device not in {"auto", "cuda", "cpu"}:
        raise MediaFailure("media_config_invalid", "device 只支援 auto、cuda 或 cpu")
    if language_raw is None or str(language_raw).strip().lower() == "auto":
        language = None
    elif not isinstance(language_raw, str) or not language_raw.strip():
        raise MediaFailure("media_config_invalid", "language 必須是語言代碼、auto 或 null")
    else:
        language = language_raw.strip()
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= MAX_MEDIA_BYTES:
        raise MediaFailure(
            "media_config_invalid",
            f"max_upload_bytes 必須是 1 到 {MAX_MEDIA_BYTES} 的整數",
        )
    return MediaConfig(model, device, language, maximum)


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path, code: str, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise MediaFailure(code, f"找不到{label}：{path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MediaFailure(code, f"{label}無法解析：{exc}") from exc
    if not isinstance(value, dict):
        raise MediaFailure(code, f"{label}根節點必須是物件")
    return value


def _resolve_executable(explicit: str | Path | None, name: str) -> Path | None:
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return candidate.resolve()
        found = shutil.which(str(explicit))
        return Path(found).resolve() if found else None
    found = shutil.which(name)
    if found:
        return Path(found).resolve()
    if os.name == "nt":
        # PATH 找不到時的候選位置。設 FFMPEG_DIR 指到你自己的 ffmpeg/ffprobe 目錄；
        # 其餘是 Windows 上常見的安裝位置。
        candidates = []
        configured = os.environ.get("FFMPEG_DIR", "").strip()
        if configured:
            candidates.append(Path(configured) / f"{name}.exe")
        candidates.extend(
            Path(base) / f"{name}.exe"
            for base in (
                # pip-installed ffmpeg lands beside the running interpreter
                Path(sys.executable).parent,
                Path(sys.executable).parent / "Scripts",
                Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links",
                r"C:\ffmpeg\bin",
                r"C:\Program Files\ffmpeg\bin",
            )
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
    return None


def _media_id_path(media_root: Path, media_id: str) -> Path:
    if not isinstance(media_id, str) or not SAFE_MEDIA_ID.fullmatch(media_id):
        raise MediaFailure("media_id_invalid", "影音 ID 格式無效")
    candidate = (media_root / media_id).resolve()
    try:
        candidate.relative_to(media_root.resolve())
    except ValueError as exc:
        raise MediaFailure("media_id_invalid", "影音 ID 路徑越界") from exc
    return candidate


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


class FasterWhisperTranscriber:
    """Lazy faster-whisper adapter with an honest CUDA-to-CPU int8 fallback."""

    def __init__(
        self,
        config: MediaConfig,
        model_factory: Callable[..., Any] | None = None,
    ):
        self.config = config
        self._model_factory = model_factory

    def _factory(self) -> Callable[..., Any]:
        if self._model_factory:
            return self._model_factory
        try:
            from faster_whisper import WhisperModel
        except (ImportError, OSError) as exc:
            raise MediaFailure("whisper_unavailable", f"faster_whisper 無法載入：{exc}") from exc
        return WhisperModel

    def _attempts(self) -> list[tuple[str, str]]:
        if self.config.device == "cpu":
            return [("cpu", "int8")]
        if self.config.device == "cuda":
            return [("cuda", "float16")]
        return [("cuda", "float16"), ("cpu", "int8")]

    def transcribe(self, source_path: Path, *, language: str | None = None) -> dict[str, Any]:
        factory = self._factory()
        failures: list[dict[str, str]] = []
        for device, compute_type in self._attempts():
            try:
                model = factory(self.config.whisper_model, device=device, compute_type=compute_type)
                segments, info = model.transcribe(str(source_path), language=language, vad_filter=True)
                # Consume the generator inside this attempt so CUDA decode failures also fall back.
                rows = [
                    {
                        "start": getattr(segment, "start", None),
                        "end": getattr(segment, "end", None),
                        "text": getattr(segment, "text", ""),
                    }
                    for segment in segments
                ]
                return {
                    "segments": rows,
                    "info": {
                        "duration": getattr(info, "duration", None),
                        "language": getattr(info, "language", language),
                        "language_probability": getattr(info, "language_probability", None),
                    },
                    "model": self.config.whisper_model,
                    "device": device,
                    "compute_type": compute_type,
                    "fallbacks": failures,
                }
            except MediaFailure:
                raise
            except Exception as exc:
                failures.append(
                    {
                        "device": device,
                        "compute_type": compute_type,
                        "message": str(exc)[:1000],
                    }
                )
        raise MediaFailure(
            "whisper_failed",
            "逐字稿模型全部嘗試失敗：" + "；".join(
                f"{row['device']}/{row['compute_type']}：{row['message']}" for row in failures
            ),
            {
                "model": self.config.whisper_model,
                "requested_device": self.config.device,
                "attempts": failures,
            },
        )


def _normalize_transcriber_result(raw: Any) -> tuple[list[Any], dict[str, Any], dict[str, Any]]:
    if isinstance(raw, dict):
        segments = raw.get("segments")
        info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
        adapter_provenance = {
            key: raw[key]
            for key in ("model", "device", "compute_type", "fallbacks")
            if key in raw
        }
    elif isinstance(raw, tuple) and len(raw) == 2:
        segments, info_raw = raw
        info = info_raw if isinstance(info_raw, dict) else {
            "duration": getattr(info_raw, "duration", None),
            "language": getattr(info_raw, "language", None),
            "language_probability": getattr(info_raw, "language_probability", None),
        }
        adapter_provenance = {}
    else:
        segments, info, adapter_provenance = raw, {}, {}
    if isinstance(segments, (str, bytes, dict)) or segments is None:
        raise MediaFailure("whisper_output_unusable", "逐字稿輸出不是可用的段落清單")
    try:
        rows = list(segments)
    except TypeError as exc:
        raise MediaFailure("whisper_output_unusable", "逐字稿輸出無法迭代") from exc
    return rows, info, adapter_provenance


def normalize_transcript_segments(
    raw_segments: Iterable[Any],
    duration: float | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    normalized: list[dict[str, Any]] = []
    maximum = max(0.0, _number(duration)) if duration is not None else None
    for index, raw in enumerate(raw_segments):
        if isinstance(raw, dict):
            start_raw, end_raw, text_raw = raw.get("start"), raw.get("end"), raw.get("text")
        else:
            start_raw = getattr(raw, "start", None)
            end_raw = getattr(raw, "end", None)
            text_raw = getattr(raw, "text", "")
        start = _number(start_raw, -1.0)
        end = _number(end_raw, -1.0)
        text = str(text_raw or "").strip()
        if not text:
            warnings.append(f"逐字稿第 {index + 1} 段沒有文字，已略過")
            continue
        if start < 0 or end <= start:
            warnings.append(f"逐字稿第 {index + 1} 段時間碼無效，已略過")
            continue
        if maximum is not None:
            clamped_start = min(max(start, 0.0), maximum)
            clamped_end = min(max(end, 0.0), maximum)
            if (clamped_start, clamped_end) != (start, end):
                warnings.append(f"逐字稿第 {index + 1} 段超出片長，已夾回有效範圍")
            start, end = clamped_start, clamped_end
            if end <= start:
                warnings.append(f"逐字稿第 {index + 1} 段夾回後沒有長度，已略過")
                continue
        normalized.append(
            {
                "id": f"segment-{len(normalized) + 1}",
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
            }
        )
    if not normalized:
        raise MediaFailure("whisper_output_unusable", "逐字稿沒有任何可用的帶時間碼段落")
    return normalized, warnings


def highlight_output_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["segments"],
        "properties": {
            "segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "segment_id",
                        "start",
                        "end",
                        "material_score",
                        "humor_score",
                        "controversy_score",
                        "reason",
                    ],
                    "properties": {
                        "segment_id": {"type": "string"},
                        "start": {"type": "number"},
                        "end": {"type": "number"},
                        "material_score": {"type": "number"},
                        "humor_score": {"type": "number"},
                        "controversy_score": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                },
            }
        },
    }


def _extract_score(raw: dict[str, Any], keys: tuple[str, ...], label: str, warnings: list[str]) -> float:
    value: Any = None
    found = False
    scores = raw.get("scores") if isinstance(raw.get("scores"), dict) else {}
    for key in keys:
        if key in raw:
            value, found = raw[key], True
            break
        if key in scores:
            value, found = scores[key], True
            break
    if not found:
        warnings.append(f"{label} 缺少，已補為 0")
        return 0.0
    parsed = _number(value, float("nan"))
    if not math.isfinite(parsed):
        warnings.append(f"{label} 不是數字，已補為 0")
        return 0.0
    clamped = min(10.0, max(0.0, parsed))
    if clamped != parsed:
        warnings.append(f"{label} 超出 0 到 10，已夾回 {clamped:g}")
    return round(clamped, 2)


def normalize_highlight_output(
    raw: Any,
    transcript_segments: list[dict[str, Any]],
    duration: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep decodable rows, warn on metadata defects, and fail only on an unreadable bundle."""
    warnings: list[str] = []
    if isinstance(raw, dict):
        rows = raw.get("segments", raw.get("highlights", raw.get("scores")))
        unknown = sorted(set(raw) - {"segments", "highlights", "scores"})
        if unknown:
            warnings.append("精華輸出未知根欄位已忽略：" + "、".join(unknown))
    elif isinstance(raw, list):
        rows = raw
        warnings.append("精華輸出根節點原為清單，已正規化")
    else:
        raise MediaFailure("highlight_output_unusable", "精華判斷輸出根節點無法解讀")
    if not isinstance(rows, list):
        raise MediaFailure("highlight_output_unusable", "精華判斷輸出沒有可解讀的段落清單")
    by_id = {str(item["id"]): item for item in transcript_segments}
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    maximum = max(0.0, _number(duration))
    for index, row in enumerate(rows):
        label = f"精華第 {index + 1} 段"
        if not isinstance(row, dict):
            warnings.append(f"{label} 不是物件，已略過")
            continue
        segment_id = str(row.get("segment_id") or row.get("id") or "").strip()
        segment = by_id.get(segment_id)
        if segment is None and isinstance(row.get("segment_index"), int):
            candidate = int(row["segment_index"])
            if 0 <= candidate < len(transcript_segments):
                segment = transcript_segments[candidate]
            elif 1 <= candidate <= len(transcript_segments):
                segment = transcript_segments[candidate - 1]
        if segment is None and index < len(transcript_segments):
            segment = transcript_segments[index]
            warnings.append(f"{label} 缺少有效 segment_id，已依輸出順序配對 {segment['id']}")
        if segment is None:
            warnings.append(f"{label} 無法配對逐字稿段落，已略過")
            continue
        segment_id = str(segment["id"])
        if segment_id in seen:
            warnings.append(f"{label} 重複評分 {segment_id}，已略過重複列")
            continue
        seen.add(segment_id)
        default_start, default_end = float(segment["start"]), float(segment["end"])
        start = _number(row.get("start"), default_start)
        end = _number(row.get("end"), default_end)
        clamped_start = min(max(start, 0.0), maximum)
        clamped_end = min(max(end, 0.0), maximum)
        if (clamped_start, clamped_end) != (start, end):
            warnings.append(f"{label}時間碼超出片長，已夾回 0 到 {maximum:g} 秒")
        if clamped_end <= clamped_start:
            warnings.append(f"{label}時間碼無效，已改回原逐字稿段落邊界")
            clamped_start = min(max(default_start, 0.0), maximum)
            clamped_end = min(max(default_end, 0.0), maximum)
        if (clamped_start, clamped_end) != (default_start, default_end):
            warnings.append(f"{label}時間碼已貼齊原逐字稿段落邊界")
            clamped_start, clamped_end = default_start, default_end
        reason = row.get("reason", row.get("理由", ""))
        if reason is None:
            reason = ""
        elif not isinstance(reason, str):
            reason = str(reason)
            warnings.append(f"{label}理由不是字串，已轉為文字")
        reason = reason.strip()
        if not reason:
            warnings.append(f"{label}缺少理由，已保留空白，不代為捏造")
        if len(reason) > 600:
            warnings.append(f"{label}理由超過 600 字，已截斷")
            reason = reason[:600]
        normalized.append(
            {
                "segment_id": segment_id,
                "start": round(clamped_start, 3),
                "end": round(clamped_end, 3),
                "text": str(segment.get("text") or ""),
                "scores": {
                    "material": _extract_score(
                        row,
                        ("material_score", "material", "valuable", "有料"),
                        f"{label}有料分數",
                        warnings,
                    ),
                    "humor": _extract_score(
                        row,
                        ("humor_score", "humor", "funny", "搞笑"),
                        f"{label}搞笑分數",
                        warnings,
                    ),
                    "controversy": _extract_score(
                        row,
                        ("controversy_score", "controversy", "controversial", "爭議"),
                        f"{label}爭議分數",
                        warnings,
                    ),
                },
                "reason": reason,
            }
        )
    if not normalized:
        raise MediaFailure("highlight_output_unusable", "精華判斷沒有任何可用段落")
    missing = [item["id"] for item in transcript_segments if item["id"] not in seen]
    if missing:
        warnings.append("語義引擎未評分這些段落：" + "、".join(missing))
    normalized.sort(key=lambda item: (float(item["start"]), float(item["end"])))
    return normalized, warnings


def _segment_strength(item: dict[str, Any]) -> tuple[float, float]:
    scores = item.get("scores") if isinstance(item.get("scores"), dict) else {}
    values = [
        _number(scores.get("material")),
        _number(scores.get("humor")),
        _number(scores.get("controversy")),
    ]
    return max(values), sum(values)


def _duration(item: dict[str, Any]) -> float:
    return max(0.0, _number(item.get("end")) - _number(item.get("start")))


def _best_distinct(
    rows: list[dict[str, Any]],
    candidates: Iterable[int],
    selected: set[int],
) -> int | None:
    available = [index for index in candidates if index not in selected]
    if not available:
        return None
    return max(available, key=lambda index: (*_segment_strength(rows[index]), -index))


def plan_clips(
    scored_segments: list[dict[str, Any]],
    duration: float,
    targets: tuple[int, ...] = TARGET_CLIP_SECONDS,
) -> list[dict[str, Any]]:
    """Pure local planner. Every selected boundary comes from a scored transcript segment."""
    if not scored_segments:
        raise ValueError("沒有已評分段落，無法產生切片計畫")
    maximum = _number(duration)
    if maximum <= 0:
        raise ValueError("影片長度必須大於 0")
    rows = sorted(
        (
            dict(item)
            for item in scored_segments
            if 0 <= _number(item.get("start")) < _number(item.get("end")) <= maximum + 1e-6
        ),
        key=lambda item: (_number(item["start"]), _number(item["end"])),
    )
    if not rows:
        raise ValueError("沒有位於影片範圍內的已評分段落")
    peak = max(range(len(rows)), key=lambda index: (*_segment_strength(rows[index]), -index))
    first_zone = range(0, max(1, math.ceil(len(rows) * 0.35)))
    last_start = min(len(rows) - 1, math.floor(len(rows) * 0.65))
    last_zone = range(last_start, len(rows))
    plans: list[dict[str, Any]] = []
    for target in targets:
        if target not in TARGET_CLIP_SECONDS:
            raise ValueError(f"不支援的目標長度：{target}")
        selected: set[int] = set()
        roles: dict[int, set[str]] = {}

        def choose(index: int | None, role: str) -> None:
            if index is not None:
                selected.add(index)
                roles.setdefault(index, set()).add(role)

        if target == 10:
            choose(peak, "最高分爆點")
        elif target == 30:
            choose(peak, "主要爆點")
            second = _best_distinct(rows, range(len(rows)), selected)
            if second is not None and _duration(rows[peak]) < target * 0.8:
                choose(second, "次高分補強")
        else:
            hook = _best_distinct(rows, first_zone, selected)
            choose(hook, "開頭鉤子")
            choose(peak, "高分主峰")
            close = _best_distinct(rows, last_zone, selected)
            choose(close, "收尾")
            # 60 秒保留精簡三幕；90 秒才加入額外脈絡，避免兩者只是同支片縮短。
            if target == 90:
                ranked = sorted(
                    (index for index in range(len(rows)) if index not in selected),
                    key=lambda index: (*_segment_strength(rows[index]), -index),
                    reverse=True,
                )
                for index in ranked:
                    current = sum(_duration(rows[item]) for item in selected)
                    candidate_duration = _duration(rows[index])
                    if current >= target * 0.8:
                        break
                    if current + candidate_duration <= target + 2:
                        choose(index, "90 秒脈絡補強")
        ordered = sorted(selected)
        timecodes = [
            {
                "segment_id": str(rows[index].get("segment_id") or f"segment-{index + 1}"),
                "start": round(_number(rows[index]["start"]), 3),
                "end": round(_number(rows[index]["end"]), 3),
                "role": "＋".join(sorted(roles.get(index, {"精華段"}))),
                "reason": str(rows[index].get("reason") or "").strip(),
            }
            for index in ordered
        ]
        actual = round(sum(item["end"] - item["start"] for item in timecodes), 3)
        peak_text = str(rows[peak].get("text") or "").strip().replace("\n", " ")
        suggestion = (peak_text[:36] + ("…" if len(peak_text) > 36 else "")) or "未提供可用標題文字"
        plans.append(
            {
                "target_seconds": target,
                "actual_seconds": actual,
                "timecodes": timecodes,
                "selection_reason": "；".join(
                    f"{item['role']} {item['start']:g}-{item['end']:g} 秒"
                    for item in timecodes
                ),
                "suggested_title": suggestion,
                "planner": "local_rules",
            }
        )
    return plans


class HighlightEngineAdapter:
    """Use the existing semantic command adapter with the media score contract."""

    class _UnusedCatalog:
        def get_asset(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("精華判斷不讀新聞角色卡")

    def __init__(self, config_path: Path, workdir: Path, temp_root: Path):
        self.config_path = config_path.resolve()
        self._semantic = SemanticEngineAdapter(
            self._UnusedCatalog(),
            self.config_path,
            workdir.resolve(),
            temp_root.resolve(),
        )

    def run(self, transcript: dict[str, Any], progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        config: EngineConfig = load_engine_config(self.config_path)
        segments = transcript.get("segments")
        if not isinstance(segments, list) or not segments:
            raise MediaFailure("transcript_unusable", "逐字稿沒有可供精華判斷的段落")
        prompt = {
            "contract_version": "wanjuan_highlight_scores_v1",
            "role": "video_highlight_judge",
            "instructions": [
                "只輸出符合 schema 的 JSON，不要 Markdown。",
                "逐段評分，不得新增逐字稿沒有的內容。",
                "有料、搞笑、爭議各為 0 到 10，可同段多項高分。",
                "每段提供一句可由逐字稿核對的理由；保留原 segment_id 與時間碼。",
                "分數是內容線索，不代表觀眾感官品質已通過。",
            ],
            "segments": segments,
            "output_schema": highlight_output_schema(),
        }
        if progress:
            progress(config.name)
        try:
            raw, invocation = self._semantic._invoke(
                config,
                "影音精華判斷",
                prompt,
                highlight_output_schema(),
            )
        except SemanticFailure as exc:
            raise MediaFailure(exc.code, exc.message, exc.provenance) from exc
        duration = _number(transcript.get("duration"))
        normalized, warnings = normalize_highlight_output(raw, segments, duration)
        planner_started = time.perf_counter()
        plans = plan_clips(normalized, duration)
        planner_elapsed_ms = round((time.perf_counter() - planner_started) * 1000)
        provenance = {
            "engine": config.name,
            "engine_config_path": str(self.config_path),
            "network_disclosure": config.network_disclosure,
            "input_hash": json_hash(prompt),
            "output_hash": json_hash({"segments": normalized, "plans": plans}),
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "invocation": invocation,
            "normalization_warnings": {"count": len(warnings), "messages": warnings},
            "planner": {
                "engine": "local_rules",
                "targets": list(TARGET_CLIP_SECONDS),
                "input_hash": json_hash(normalized),
                "output_hash": json_hash(plans),
                "elapsed_ms": planner_elapsed_ms,
            },
        }
        return {
            "contract_version": MEDIA_CONTRACT_VERSION,
            "workflow": {"id": "media_highlights", "status": "complete"},
            "segments": normalized,
            "plans": plans,
            "summary": {"segments": len(normalized), "plans": len(plans)},
            "provenance": provenance,
        }


def probe_media(path: Path, ffprobe_path: Path | None, ffmpeg_path: Path | None) -> dict[str, Any]:
    if ffprobe_path and ffprobe_path.is_file():
        command = [
            str(ffprobe_path),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-show_entries",
            "stream=codec_type,codec_name",
            "-of",
            "json",
            str(path),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        if completed.returncode != 0:
            raise MediaFailure("ffprobe_failed", f"ffprobe 無法讀取影音：{(completed.stderr or completed.stdout).strip()[:1000]}")
        try:
            raw = json.loads(completed.stdout)
            duration = float(raw["format"]["duration"])
            stream_rows = [item for item in raw.get("streams", []) if isinstance(item, dict)]
            streams = {str(item.get("codec_type")) for item in stream_rows}
            codecs = {
                str(item.get("codec_type")): str(item.get("codec_name") or "")
                for item in stream_rows
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MediaFailure("ffprobe_output_invalid", f"ffprobe 輸出無法解析：{exc}") from exc
        return {
            "duration": duration,
            "has_video": "video" in streams,
            "has_audio": "audio" in streams,
            "codecs": codecs,
            "method": "ffprobe",
            "warnings": [],
        }
    if not ffmpeg_path or not ffmpeg_path.is_file():
        raise MediaFailure("ffprobe_unavailable", "找不到 ffprobe，且沒有 ffmpeg 可作誠實退路探測")
    command = [str(ffmpeg_path), "-hide_banner", "-i", str(path), "-f", "null", "-"]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    diagnostic = f"{completed.stdout}\n{completed.stderr}"
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", diagnostic)
    if not duration_match:
        raise MediaFailure("media_probe_failed", "ffmpeg 無法讀取影音長度")
    hours, minutes, seconds = duration_match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return {
        "duration": duration,
        "has_video": bool(re.search(r"Stream.*Video:", diagnostic)),
        "has_audio": bool(re.search(r"Stream.*Audio:", diagnostic)),
        "method": "ffmpeg_fallback",
        "warnings": ["本機找不到 ffprobe，已以 ffmpeg 解碼探測長度；尚未達成 ffprobe 驗證條件"],
    }


def _ffmpeg_cut_command(
    ffmpeg: Path,
    source: Path,
    output: Path,
    timecodes: list[dict[str, Any]],
    has_video: bool,
    has_audio: bool,
) -> list[str]:
    if not has_video and not has_audio:
        raise MediaFailure("media_stream_missing", "來源沒有可切割的影音串流")
    if len(timecodes) == 1:
        start = _number(timecodes[0].get("start"))
        end = _number(timecodes[0].get("end"))
        command = [
            str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
        ]
        if has_video:
            command += ["-c:v", "libx264", "-preset", "veryfast"]
        else:
            command += ["-vn"]
        if has_audio:
            command += ["-c:a", "aac"]
        else:
            command += ["-an"]
        return command + ["-movflags", "+faststart", str(output)]

    filters: list[str] = []
    labels: list[str] = []
    for index, timecode in enumerate(timecodes):
        start = _number(timecode.get("start"))
        end = _number(timecode.get("end"))
        if has_video:
            filters.append(f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{index}]")
            labels.append(f"[v{index}]")
        if has_audio:
            filters.append(f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{index}]")
            labels.append(f"[a{index}]")
    filters.append(
        "".join(labels)
        + f"concat=n={len(timecodes)}:v={1 if has_video else 0}:a={1 if has_audio else 0}"
        + ("[vout]" if has_video else "")
        + ("[aout]" if has_audio else "")
    )
    command = [
        str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-filter_complex", ";".join(filters),
    ]
    if has_video:
        command += ["-map", "[vout]", "-c:v", "libx264", "-preset", "veryfast"]
    if has_audio:
        command += ["-map", "[aout]", "-c:a", "aac"]
    return command + ["-movflags", "+faststart", str(output)]


class MediaPipeline:
    def __init__(
        self,
        project_root: Path,
        transcriber: Transcriber | Callable[..., Any] | None = None,
        ffmpeg_path: str | Path | None = None,
        ffprobe_path: str | Path | None = None,
        highlight_adapter: HighlightEngineAdapter | None = None,
        media_root: str | Path | None = None,
    ):
        self.project_root = project_root.resolve()
        self.data_root = self.project_root / "data"
        self.media_root = Path(media_root).resolve() if media_root else self.data_root / "media"
        self.config_path = self.data_root / "media_config.json"
        self.engine_config_path = self.data_root / "engine.json"
        self.temp_root = self.data_root / "tmp" / "media"
        self.config = load_media_config(self.config_path)
        self.transcriber = transcriber or FasterWhisperTranscriber(self.config)
        self.ffmpeg_path = _resolve_executable(ffmpeg_path, "ffmpeg")
        resolved_probe = _resolve_executable(ffprobe_path, "ffprobe")
        if resolved_probe is None and self.ffmpeg_path:
            sibling = self.ffmpeg_path.with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
            resolved_probe = sibling.resolve() if sibling.is_file() else None
        self.ffprobe_path = resolved_probe
        self.highlight_adapter = highlight_adapter or HighlightEngineAdapter(
            self.engine_config_path,
            self.project_root,
            self.temp_root,
        )
        self.media_root.mkdir(parents=True, exist_ok=True)

    def _directory(self, media_id: str, must_exist: bool = True) -> Path:
        directory = _media_id_path(self.media_root, media_id)
        if must_exist and not (directory / "media.json").is_file():
            raise MediaFailure("media_not_found", "找不到影音")
        return directory

    def create_upload(
        self,
        filename: str,
        chunks: Iterable[bytes],
        content_length: int | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        clean_name = Path(str(filename or "").replace("\\", "/")).name
        extension = Path(clean_name).suffix.lower()
        if not clean_name or extension not in ALLOWED_MEDIA_EXTENSIONS:
            raise MediaFailure(
                "media_type_not_allowed",
                "影音格式只接受 mp4、mov、mkv、m4a、mp3、wav",
            )
        if content_length is not None:
            if isinstance(content_length, bool) or not isinstance(content_length, int) or content_length < 0:
                raise MediaFailure("media_length_invalid", "Content-Length 無效")
            if content_length > self.config.max_upload_bytes:
                raise MediaFailure("media_too_large", "影音超過 3GB 上限")
        media_id = uuid.uuid4().hex
        directory = self._directory(media_id, must_exist=False)
        directory.mkdir(parents=True, exist_ok=False)
        source_name = f"source{extension}"
        source = directory / source_name
        partial = directory / f".{source_name}.uploading"
        size = 0
        digest = hashlib.sha256()
        try:
            with partial.open("xb") as handle:
                for chunk in chunks:
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise MediaFailure("media_upload_invalid", "上傳串流包含非位元組資料")
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > self.config.max_upload_bytes:
                        raise MediaFailure("media_too_large", "影音超過 3GB 上限")
                    handle.write(chunk)
                    digest.update(chunk)
            if size == 0:
                raise MediaFailure("media_empty", "上傳影音是空檔")
            if content_length is not None and size != content_length:
                raise MediaFailure(
                    "media_length_mismatch",
                    f"實收 {size} bytes，與 Content-Length {content_length} 不符",
                )
            os.replace(partial, source)
            metadata = {
                "contract_version": MEDIA_CONTRACT_VERSION,
                "id": media_id,
                "original_filename": clean_name,
                "source_filename": source_name,
                "size_bytes": size,
                "created_at": utc_now(),
                "provenance": {
                    "source_sha256": digest.hexdigest(),
                    "elapsed_ms": round((time.perf_counter() - started) * 1000),
                    "streamed_to_disk": True,
                    "max_upload_bytes": self.config.max_upload_bytes,
                },
            }
            _write_json_atomic(directory / "media.json", metadata)
            return self.get_media(media_id)
        except Exception:
            partial.unlink(missing_ok=True)
            if not (directory / "media.json").exists():
                shutil.rmtree(directory, ignore_errors=True)
            raise

    def get_media(self, media_id: str) -> dict[str, Any]:
        directory = self._directory(media_id)
        metadata = _read_json(directory / "media.json", "media_not_found", "影音資料")
        result = dict(metadata)
        for key, filename in (
            ("transcript", "transcript.json"),
            ("highlights", "highlights.json"),
            ("clips", "clips.json"),
        ):
            path = directory / filename
            result[key] = _read_json(path, f"{key}_invalid", key) if path.is_file() else None
        return result

    def resolve_source(self, media_id: str) -> Path:
        directory = self._directory(media_id)
        metadata = _read_json(directory / "media.json", "media_not_found", "影音資料")
        filename = str(metadata.get("source_filename") or "")
        source = (directory / filename).resolve()
        try:
            source.relative_to(directory)
        except ValueError as exc:
            raise MediaFailure("media_source_invalid", "來源影音路徑越界") from exc
        if not source.is_file():
            raise MediaFailure("media_source_missing", "來源影音檔不存在")
        return source

    def resolve_clip(self, media_id: str, filename: str) -> Path:
        if not isinstance(filename, str) or not SAFE_CLIP_NAME.fullmatch(filename):
            raise MediaFailure("clip_name_invalid", "短片檔名格式無效")
        directory = self._directory(media_id)
        path = (directory / "clips" / filename).resolve()
        try:
            path.relative_to(directory / "clips")
        except ValueError as exc:
            raise MediaFailure("clip_name_invalid", "短片路徑越界") from exc
        if not path.is_file():
            raise MediaFailure("clip_not_found", "找不到短片")
        return path

    def _call_transcriber(self, source: Path) -> Any:
        target = self.transcriber
        if hasattr(target, "transcribe"):
            return target.transcribe(source, language=self.config.language)  # type: ignore[union-attr]
        if callable(target):
            return target(source, language=self.config.language)
        raise MediaFailure("whisper_unavailable", "轉寫器不可呼叫")

    def transcribe(self, media_id: str) -> dict[str, Any]:
        started = time.perf_counter()
        directory = self._directory(media_id)
        source = self.resolve_source(media_id)
        source_hash = _sha256_file(source)
        raw = self._call_transcriber(source)
        raw_segments, info, adapter_provenance = _normalize_transcriber_result(raw)
        reported_duration = _number(info.get("duration"))
        if reported_duration <= 0:
            probe = probe_media(source, self.ffprobe_path, self.ffmpeg_path)
            reported_duration = _number(probe["duration"])
            probe_warnings = list(probe.get("warnings") or [])
        else:
            probe = None
            probe_warnings = []
        segments, warnings = normalize_transcript_segments(raw_segments, reported_duration)
        warnings.extend(probe_warnings)
        text = "\n".join(item["text"] for item in segments)
        payload = {
            "contract_version": MEDIA_CONTRACT_VERSION,
            "workflow": {"id": "media_transcription", "status": "complete"},
            "media_id": media_id,
            "duration": round(reported_duration, 3),
            "language": info.get("language", self.config.language),
            "text": text,
            "segments": segments,
            "summary": {"segments": len(segments), "characters": len(text)},
            "provenance": {
                "model": adapter_provenance.get("model", self.config.whisper_model),
                "requested_device": self.config.device,
                "actual_device": adapter_provenance.get("device", "injected"),
                "compute_type": adapter_provenance.get("compute_type", "injected"),
                "language_requested": self.config.language,
                "source_sha256": source_hash,
                "input_hash": source_hash,
                "output_hash": json_hash({"duration": reported_duration, "segments": segments}),
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "fallbacks": adapter_provenance.get("fallbacks", []),
                "duration_probe": probe,
                "normalization_warnings": {"count": len(warnings), "messages": warnings},
            },
        }
        _write_json_atomic(directory / "transcript.json", payload)
        return payload

    def judge_highlights(self, media_id: str, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        directory = self._directory(media_id)
        transcript = _read_json(directory / "transcript.json", "transcript_missing", "逐字稿")
        result = self.highlight_adapter.run(transcript, progress=progress)
        result["media_id"] = media_id
        result["provenance"]["transcript_hash"] = json_hash(transcript)
        _write_json_atomic(directory / "highlights.json", result)
        return result

    def cut_clips(self, media_id: str, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        if not self.ffmpeg_path:
            raise MediaFailure("ffmpeg_unavailable", "找不到 ffmpeg，不能切短片")
        if not self.ffprobe_path:
            raise MediaFailure("ffprobe_unavailable", "找不到 ffprobe，不能完成短片長度驗證")
        directory = self._directory(media_id)
        source = self.resolve_source(media_id)
        highlights = _read_json(directory / "highlights.json", "highlights_missing", "精華計畫")
        plans = highlights.get("plans")
        if not isinstance(plans, list) or not plans:
            raise MediaFailure("clip_plan_unusable", "精華計畫沒有可切割方案")
        source_probe = probe_media(source, self.ffprobe_path, self.ffmpeg_path)
        clips_directory = directory / "clips"
        staging = directory / f".clips-{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        clips: list[dict[str, Any]] = []
        try:
            for index, plan in enumerate(plans):
                if not isinstance(plan, dict):
                    raise MediaFailure("clip_plan_unusable", f"第 {index + 1} 個切片計畫不是物件")
                target = plan.get("target_seconds")
                if isinstance(target, bool) or not isinstance(target, int) or target not in TARGET_CLIP_SECONDS:
                    raise MediaFailure("clip_plan_unusable", f"第 {index + 1} 個切片目標長度無效")
                timecodes = plan.get("timecodes")
                if not isinstance(timecodes, list) or not timecodes:
                    raise MediaFailure("clip_plan_unusable", f"{target} 秒計畫沒有時間碼")
                filename = f"clip_{target}s_1.mp4"
                staged_path = staging / filename
                command = _ffmpeg_cut_command(
                    self.ffmpeg_path,
                    source,
                    staged_path,
                    timecodes,
                    bool(source_probe["has_video"]),
                    bool(source_probe["has_audio"]),
                )
                if progress:
                    progress(f"{target}s")
                invocation_started = time.perf_counter()
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                invocation_elapsed = round((time.perf_counter() - invocation_started) * 1000)
                if completed.returncode != 0 or not staged_path.is_file():
                    detail = (completed.stderr or completed.stdout).strip()[:1200]
                    raise MediaFailure(
                        "ffmpeg_cut_failed",
                        f"{target} 秒短片切割失敗（exit {completed.returncode}）：{detail or '沒有錯誤輸出'}",
                        {"target_seconds": target, "elapsed_ms": invocation_elapsed},
                    )
                output_probe = probe_media(staged_path, self.ffprobe_path, self.ffmpeg_path)
                if output_probe.get("method") != "ffprobe":
                    raise MediaFailure("ffprobe_unavailable", "短片必須由 ffprobe 驗證長度")
                expected = sum(
                    _number(item.get("end")) - _number(item.get("start"))
                    for item in timecodes
                    if isinstance(item, dict)
                )
                measured = _number(output_probe["duration"])
                if abs(measured - expected) > 2.0:
                    raise MediaFailure(
                        "clip_duration_mismatch",
                        f"{target} 秒短片實測 {measured:.3f} 秒，與計畫 {expected:.3f} 秒相差超過 2 秒",
                        {"target_seconds": target, "expected_seconds": expected, "measured_seconds": measured},
                    )
                clips.append(
                    {
                        "target_seconds": target,
                        "planned_seconds": round(expected, 3),
                        "measured_seconds": round(measured, 3),
                        "filename": filename,
                        "sha256": _sha256_file(staged_path),
                        "size_bytes": staged_path.stat().st_size,
                        "suggested_title": str(plan.get("suggested_title") or ""),
                        "selection_reason": str(plan.get("selection_reason") or ""),
                        "sensory_label": "吸不吸引人，感官未判，待人工裁定",
                        "verification": output_probe,
                        "provenance": {
                            "engine": "ffmpeg",
                            "video_codec": "libx264" if source_probe["has_video"] else None,
                            "preset": "veryfast" if source_probe["has_video"] else None,
                            "audio_codec": "aac" if source_probe["has_audio"] else None,
                            "elapsed_ms": invocation_elapsed,
                            "command_hash": json_hash(command),
                        },
                    }
                )
            clips_directory.mkdir(parents=True, exist_ok=True)
            for clip in clips:
                os.replace(staging / clip["filename"], clips_directory / clip["filename"])
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        warnings = list(source_probe.get("warnings") or [])
        for clip in clips:
            warnings.extend(clip["verification"].get("warnings") or [])
        payload = {
            "contract_version": MEDIA_CONTRACT_VERSION,
            "workflow": {"id": "media_clips", "status": "complete"},
            "media_id": media_id,
            "clips": clips,
            "summary": {"clips": len(clips)},
            "provenance": {
                "engine": "ffmpeg",
                "source_sha256": _sha256_file(source),
                "input_hash": json_hash(plans),
                "output_hash": json_hash([{key: item[key] for key in ("filename", "sha256", "measured_seconds")} for item in clips]),
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "normalization_warnings": {"count": len(warnings), "messages": warnings},
            },
        }
        _write_json_atomic(directory / "clips.json", payload)
        return payload


class MediaJobManager:
    def __init__(self, pipeline: MediaPipeline):
        self.pipeline = pipeline
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def start_transcription(self, media_id: str) -> dict[str, Any]:
        return self.start(media_id, "transcribe")

    def start_highlights(self, media_id: str) -> dict[str, Any]:
        return self.start(media_id, "highlights")

    def start_clips(self, media_id: str) -> dict[str, Any]:
        return self.start(media_id, "clips")

    def start(self, media_id: str, action: str) -> dict[str, Any]:
        actions = {
            "transcribe": self.pipeline.transcribe,
            "highlights": self.pipeline.judge_highlights,
            "clips": self.pipeline.cut_clips,
        }
        worker = actions.get(action)
        if worker is None:
            raise ValueError("影音工作只支援 transcribe、highlights、clips")
        self.pipeline._directory(media_id)
        with self._lock:
            for job in self._jobs.values():
                if job["media_id"] == media_id and job["action"] == action and job["status"] in {"queued", "running"}:
                    return dict(job)
            job_id = uuid.uuid4().hex
            job = {
                "id": job_id,
                "media_id": media_id,
                "action": action,
                "status": "queued",
                "pass": "queued",
                "created_at": utc_now(),
                "started_at": None,
                "completed_at": None,
                "result": None,
                "error": None,
            }
            self._jobs[job_id] = job
        threading.Thread(
            target=self._run,
            args=(job_id, action, worker, media_id),
            name=f"media-{action}-{job_id[:8]}",
            daemon=True,
        ).start()
        return dict(job)

    def _update(self, job_id: str, **values: Any) -> None:
        with self._lock:
            self._jobs[job_id].update(values)

    def _run(self, job_id: str, action: str, worker: Callable[..., dict[str, Any]], media_id: str) -> None:
        self._update(job_id, status="running", started_at=utc_now(), **{"pass": action})
        try:
            if action in {"highlights", "clips"}:
                result = worker(media_id, progress=lambda stage: self._update(job_id, **{"pass": stage}))
            else:
                result = worker(media_id)
            self._update(
                job_id,
                status="complete",
                completed_at=utc_now(),
                result=result,
                **{"pass": "complete"},
            )
        except MediaFailure as exc:
            self._update(
                job_id,
                status="failed",
                completed_at=utc_now(),
                error={"code": exc.code, "message": exc.message, "provenance": exc.provenance},
                **{"pass": "failed"},
            )
        except Exception as exc:
            self._update(
                job_id,
                status="failed",
                completed_at=utc_now(),
                error={"code": "media_internal_error", "message": f"影音背景工作失敗：{exc}"},
                **{"pass": "failed"},
            )

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError("找不到影音工作")
            return dict(job)

    def latest(self, media_id: str, action: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            candidates = [
                dict(job)
                for job in self._jobs.values()
                if job["media_id"] == media_id and (action is None or job["action"] == action)
            ]
        return candidates[-1] if candidates else None
