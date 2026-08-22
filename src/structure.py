from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable


STRUCTURE_TYPES = ("main_title", "subtitle", "lead", "subheading", "body")
STRUCTURE_LABELS = {
    "main_title": "主標",
    "subtitle": "副標",
    "lead": "前言",
    "subheading": "小標",
    "body": "正文段",
}
_TERMINAL_PUNCTUATION = re.compile(r"[。！？!?；;：:]\s*$")
_PARAGRAPH = re.compile(r"\S(?:.*?\S)?(?=(?:\r?\n[ \t]*\r?\n)|\Z)", re.DOTALL)


class StructureError(ValueError):
    """The submitted structure no longer matches the immutable revision text."""


def _trimmed_span(content: str, start: int, end: int) -> tuple[int, int, str]:
    raw = content[start:end]
    left = len(raw) - len(raw.lstrip())
    right = len(raw.rstrip())
    clean_start = start + left
    clean_end = start + right
    return clean_start, clean_end, content[clean_start:clean_end]


def _block_id(start: int, end: int, text: str) -> str:
    digest = hashlib.sha256(f"{start}\0{end}\0{text}".encode("utf-8")).hexdigest()[:12]
    return f"block-{digest}"


def _block(kind: str, start: int, end: int, text: str, index: int) -> dict[str, Any]:
    return {
        "id": _block_id(start, end, text),
        "type": kind,
        "label": STRUCTURE_LABELS[kind],
        "text": text,
        "start": start,
        "end": end,
        "index": index,
        "source": "rule",
    }


def _short_heading(text: str, limit: int) -> bool:
    compact = re.sub(r"\s+", "", text)
    return bool(compact) and len(compact) <= limit and not _TERMINAL_PUNCTUATION.search(compact)


def _paragraph_spans(content: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for match in _PARAGRAPH.finditer(content):
        start, end, text = _trimmed_span(content, match.start(), match.end())
        if text:
            spans.append((start, end, text))
    return spans


def _first_line_span(content: str, start: int, end: int) -> tuple[int, int, str]:
    line_end_match = re.search(r"\r?\n", content[start:end])
    line_end = start + line_end_match.start() if line_end_match else end
    return _trimmed_span(content, start, line_end)


def detect_structure(content: str) -> dict[str, Any]:
    """Detect newsroom blocks without modifying or normalising the source text."""

    paragraphs = _paragraph_spans(content)
    blocks: list[dict[str, Any]] = []
    body_number = 0
    if not paragraphs:
        return {
            "contract_version": "newsroom_structure_v2",
            "blocks": [],
            "summary": {kind: 0 for kind in STRUCTURE_TYPES},
        }

    first_start, first_end, first_text = paragraphs[0]
    first_line_start, first_line_end, first_line = _first_line_span(content, first_start, first_end)
    paragraph_cursor = 0
    content_cursor = first_start

    if _short_heading(first_line, 44):
        blocks.append(_block("main_title", first_line_start, first_line_end, first_line, len(blocks)))
        content_cursor = first_line_end

        remainder = content[first_line_end:first_end]
        line_break = re.match(r"\r?\n([^\r\n]+)", remainder)
        if line_break:
            candidate_start = first_line_end + line_break.start(1)
            candidate_end = first_line_end + line_break.end(1)
            candidate_start, candidate_end, candidate = _trimmed_span(
                content, candidate_start, candidate_end
            )
            if _short_heading(candidate, 56):
                blocks.append(
                    _block("subtitle", candidate_start, candidate_end, candidate, len(blocks))
                )
                content_cursor = candidate_end

        remainder_start, remainder_end, remainder_text = _trimmed_span(
            content, content_cursor, first_end
        )
        if remainder_text:
            blocks.append(
                _block("lead", remainder_start, remainder_end, remainder_text, len(blocks))
            )
        paragraph_cursor = 1

        if len(paragraphs) > 1 and not any(item["type"] == "lead" for item in blocks):
            lead_start, lead_end, lead_text = paragraphs[1]
            blocks.append(_block("lead", lead_start, lead_end, lead_text, len(blocks)))
            paragraph_cursor = 2
    else:
        blocks.append(_block("lead", first_start, first_end, first_text, len(blocks)))
        paragraph_cursor = 1

    remaining = paragraphs[paragraph_cursor:]
    for position, (start, end, text) in enumerate(remaining):
        is_single_line = not re.search(r"\r?\n", text)
        has_following_paragraph = position < len(remaining) - 1
        if is_single_line and has_following_paragraph and _short_heading(text, 20):
            kind = "subheading"
        else:
            kind = "body"
            body_number += 1
        block = _block(kind, start, end, text, len(blocks))
        if kind == "body":
            block["body_number"] = body_number
        blocks.append(block)

    summary = {kind: sum(item["type"] == kind for item in blocks) for kind in STRUCTURE_TYPES}
    return {
        "contract_version": "newsroom_structure_v2",
        "blocks": blocks,
        "summary": summary,
    }


def apply_structure_overrides(
    content: str,
    detected: dict[str, Any],
    overrides: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Apply human labels while keeping block text and coordinates immutable."""

    blocks = [dict(item) for item in detected.get("blocks", [])]
    by_id = {item["id"]: item for item in blocks}
    submitted: set[str] = set()
    for raw in overrides:
        if not isinstance(raw, dict):
            raise StructureError("結構改判格式錯誤")
        block_id = str(raw.get("id") or "")
        kind = str(raw.get("type") or "")
        if block_id in submitted:
            raise StructureError("同一區塊不可重複改判")
        submitted.add(block_id)
        block = by_id.get(block_id)
        if not block:
            raise StructureError("結構區塊已過期，請重新載入目前版本")
        if kind not in STRUCTURE_TYPES:
            raise StructureError("不支援的結構類型")
        if content[block["start"] : block["end"]] != block["text"]:
            raise StructureError("結構座標與目前版本不一致")
        block["type"] = kind
        block["label"] = STRUCTURE_LABELS[kind]
        block["source"] = "human"

    body_number = 0
    for index, block in enumerate(blocks):
        block["index"] = index
        block.pop("body_number", None)
        if block["type"] == "body":
            body_number += 1
            block["body_number"] = body_number
    summary = {kind: sum(item["type"] == kind for item in blocks) for kind in STRUCTURE_TYPES}
    return {
        "contract_version": "newsroom_structure_v2",
        "blocks": blocks,
        "summary": summary,
    }
